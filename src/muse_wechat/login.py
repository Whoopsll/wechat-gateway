"""扫码登录状态机和账号凭据持久化。"""

import datetime
import json
import os
import re
import time
import urllib.request
import urllib.error
import urllib.parse
from .api import _curl_request, random_uin
from .config import ACCOUNT_ID, BASE_URL, ILINK_APP_CLIENT_VERSION, ILINK_APP_ID, STATE_DIR, WATCH_DIR
from .qrcode import _write_qr_png

# ---------- 扫码登录（移植自 OpenClaw 微信插件 src/auth/login-qr.ts） ----------
LOGIN_QR_PATH = os.path.join(WATCH_DIR, "login-qr.png")
LOGIN_LOG_PATH = os.path.join(WATCH_DIR, "login.log")
LOGIN_STATE_PATH = os.path.join(WATCH_DIR, "login-state.json")
LOGIN_BOT_TYPE = "3"            # 插件 DEFAULT_ILINK_BOT_TYPE
LOGIN_POLL_TIMEOUT = 35         # 状态长轮询的客户端超时（秒），与插件 QR_LONG_POLL_TIMEOUT_MS 一致
LOGIN_MAX_QR = 3                # 一次登录最多出几张码（初码+刷新），与插件 MAX_QR_REFRESH_COUNT 语义一致
LOGIN_TIMEOUT_MINUTES = 8       # 整体等待上限 480s，与插件 channel 登录的 timeoutMs 一致

def _login_log(msg):
    """登录日志：同时打到 stdout 和 login.log；绝不打印 token 等凭据"""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [login] {msg}"
    print(line, flush=True)
    try:
        with open(LOGIN_LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass

def _login_post_headers():
    """取码 POST 的请求头：与插件 buildHeaders（无 token 时）一致，不带 Authorization"""
    return {"Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": random_uin(),
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION)}

def _login_get_headers():
    """状态轮询 GET 的请求头：与插件 buildCommonHeaders 一致"""
    return {"iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION)}

def fetch_login_qrcode():
    """取二维码，返回 {qrcode, qrcode_img_content}；失败抛异常。
    qrcode 是轮询用的 ID，qrcode_img_content 是要编码进二维码图片的链接"""
    _, _, resp = _curl_request(
        "POST", f"{BASE_URL}/ilink/bot/get_bot_qrcode?bot_type={LOGIN_BOT_TYPE}",
        headers=_login_post_headers(),
        data=json.dumps({"local_token_list": list_local_bot_tokens()}).encode(),
        timeout=30)
    data = json.loads(resp.decode())
    if not data.get("qrcode") or not data.get("qrcode_img_content"):
        raise RuntimeError(f"get_bot_qrcode 返回缺少字段: {sorted(data)}")
    return data

def poll_login_status(base_url, qrcode, verify_code=None):
    """轮询一次扫码状态（服务端长轮询，最长 35 秒）。
    与插件一致：客户端超时/网关错误一律吞掉，按 {"status": "wait"} 继续"""
    url = (base_url.rstrip("/") + "/ilink/bot/get_qrcode_status?qrcode="
           + urllib.parse.quote(qrcode, safe=""))
    if verify_code:
        url += "&verify_code=" + urllib.parse.quote(verify_code, safe="")
    try:
        _, _, resp = _curl_request("GET", url, headers=_login_get_headers(),
                                   timeout=LOGIN_POLL_TIMEOUT)
        return json.loads(resp.decode())
    except Exception as e:
        _login_log(f"轮询状态出错，按未扫码继续: {type(e).__name__}: {e}")
        return {"status": "wait"}

def normalize_account_id(raw):
    """与 plugin-sdk 的 normalizeAccountId 一致：小写，非法字符段换成 -，去首尾 -，最长 64。
    例：xxxxxxxxxxxx@im.bot 归一化为 xxxxxxxxxxxx-im-bot；归一化不出结果时回退 "default" """
    s = (raw or "").strip().lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", s):
        return s
    return re.sub(r"[^a-z0-9_-]+", "-", s).lstrip("-").rstrip("-")[:64] or "default"

def _iso_utc_now():
    """JS 的 new Date().toISOString() 同款格式，如 2026-10-15T10:31:52.374Z"""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

def _accounts_index_path():
    return os.path.join(os.path.dirname(STATE_DIR), "accounts.json")

def _read_account_file(account_id):
    try:
        with open(os.path.join(STATE_DIR, f"{account_id}.json")) as f:
            return json.load(f)
    except Exception:
        return None

def _account_index_ids():
    try:
        with open(_accounts_index_path()) as f:
            ids = json.load(f)
        return [i for i in ids if isinstance(i, str) and i.strip()] if isinstance(ids, list) else []
    except Exception:
        return []

def list_local_bot_tokens(limit=10):
    """本地已登录账号的 token（索引里最新注册的在前，最多 limit 个）。
    取码时随请求发给服务器，让它能判断扫码的 bot 是否已经绑定过（binded_redirect）"""
    tokens = []
    for aid in reversed(_account_index_ids()):
        data = _read_account_file(aid)
        token = ((data or {}).get("token") or "").strip()
        if token:
            tokens.append(token)
        if len(tokens) >= limit:
            break
    return tokens

def save_login_credentials(raw_bot_id, bot_token, base_url=None, user_id=None):
    """登录成功落盘，与插件 saveWeixinAccount + registerWeixinAccountId 一致：
    写 accounts/<normalizedId>.json（{token, savedAt, baseUrl, userId}，合并旧值，600 权限），
    并把 normalizedId 登记进 openclaw-weixin/accounts.json 索引。返回 normalizedId。
    .sync.json / .context-tokens.json 是运行态文件，登录不碰。
    token 只进文件：不进日志、不进返回值"""
    nid = normalize_account_id(raw_bot_id)
    os.makedirs(STATE_DIR, exist_ok=True)
    existing = _read_account_file(nid) or {}
    data = {}
    token = (bot_token or "").strip() or existing.get("token")
    if token:
        data["token"] = token
        data["savedAt"] = _iso_utc_now()
    base = (base_url or "").strip() or existing.get("baseUrl")
    if base:
        data["baseUrl"] = base
    if user_id is not None:
        if user_id.strip():
            data["userId"] = user_id.strip()
    elif existing.get("userId"):
        data["userId"] = existing["userId"]
    path = os.path.join(STATE_DIR, f"{nid}.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(path, 0o600)
    ids = _account_index_ids()
    if nid not in ids:
        ids.append(nid)
        with open(_accounts_index_path(), "w") as f:
            json.dump(ids, f, ensure_ascii=False, indent=2)
    return nid

def _save_login_state(state):
    try:
        fd = os.open(LOGIN_STATE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.chmod(LOGIN_STATE_PATH, 0o600)
    except OSError:
        pass

def qr_login(timeout_minutes=LOGIN_TIMEOUT_MINUTES, input_fn=None, sleep_fn=None):
    """扫码登录全流程：取码 → 生成 PNG → 轮询状态机 → 落盘。
    状态机与插件 waitForWeixinLogin 对齐：
      wait 继续轮询；scaned 已扫待确认；need_verifycode 从 stdin 读配对码后带上继续；
      expired / verify_code_blocked 刷新二维码（总共最多 LOGIN_MAX_QR 张）；
      scaned_but_redirect 把轮询主机换成 redirect_host；binded_redirect 视为已登录（不发新凭据）；
      confirmed 落盘。返回 {"result": "connected"/"already_connected"/"failed", ...}"""
    input_fn = input_fn or input
    sleep_fn = sleep_fn or time.sleep
    deadline = time.time() + max(1.0, float(timeout_minutes) * 60)
    _login_log(f"开始扫码登录，等待上限 {timeout_minutes} 分钟")
    state = {"status": "starting", "started_at": time.time()}
    _save_login_state(state)
    box = {"qrcode": "", "count": 0}

    def new_qrcode():
        resp = fetch_login_qrcode()
        box["count"] += 1
        box["qrcode"] = resp["qrcode"]
        try:
            _write_qr_png(resp["qrcode_img_content"], LOGIN_QR_PATH)
            where = LOGIN_QR_PATH
        except Exception as e:
            where = f"PNG 生成失败（{e}），改用链接：{resp['qrcode_img_content']}"
        state.update({"status": "wait", "qrcode": resp["qrcode"],
                      "qrcode_img_content": resp["qrcode_img_content"],
                      "refreshed_at": time.time()})
        _save_login_state(state)
        _login_log(f"二维码已生成（第 {box['count']}/{LOGIN_MAX_QR} 张）：{where}，请用手机微信扫码")

    def fail(message):
        state.update({"status": "failed", "message": message})
        _save_login_state(state)
        _login_log(f"登录失败：{message}")
        return {"result": "failed", "message": message}

    try:
        new_qrcode()
    except Exception as e:
        return fail(f"取二维码失败: {type(e).__name__}: {e}")
    base = BASE_URL
    pending_code = None
    scanned = False
    while time.time() < deadline:
        st = poll_login_status(base, box["qrcode"], pending_code)
        status = st.get("status")
        if status == "wait":
            pass
        elif status == "scaned":
            if pending_code:
                pending_code = None
                _login_log("配对码已接受，继续等待确认")
            if not scanned:
                scanned = True
                _login_log("已扫码，正在验证…")
        elif status == "need_verifycode":
            _login_log("需要配对码验证（手机微信上会显示一串数字）")
            prompt = ("你输入的数字不匹配，请重新输入：" if pending_code
                      else "输入手机微信显示的数字，以继续连接：")
            try:
                code = input_fn(prompt)
            except EOFError:
                return fail("需要配对码，但 stdin 已关闭")
            pending_code = (code or "").strip()
            continue                                # 带上配对码立即再轮询，不等 1 秒
        elif status in ("expired", "verify_code_blocked"):
            pending_code = None
            if status == "verify_code_blocked":
                _login_log("配对码多次输入错误")
            if box["count"] >= LOGIN_MAX_QR:
                return fail("二维码多次失效，连接流程已停止，请稍后再试")
            _login_log("二维码已过期，正在刷新…" if status == "expired" else "正在刷新二维码…")
            try:
                new_qrcode()
            except Exception as e:
                return fail(f"刷新二维码失败: {type(e).__name__}: {e}")
            scanned = False
        elif status == "scaned_but_redirect":
            host = st.get("redirect_host")
            if host:
                base = f"https://{host}"
                _login_log(f"轮询主机切换为 {host}")
            else:
                _login_log("收到 scaned_but_redirect 但没有 redirect_host，继续用当前主机")
        elif status == "binded_redirect":
            state.update({"status": "binded_redirect"})
            _save_login_state(state)
            _login_log("此 bot 已连接过，无需重复连接，本地凭据保持有效")
            return {"result": "already_connected"}
        elif status == "confirmed":
            if not st.get("ilink_bot_id"):
                return fail("服务器未返回 ilink_bot_id")
            if not st.get("bot_token"):
                return fail("服务器未返回 bot_token")
            nid = save_login_credentials(st["ilink_bot_id"], st["bot_token"],
                                         st.get("baseurl"), st.get("ilink_user_id"))
            state.update({"status": "confirmed", "account_id": nid})
            _save_login_state(state)
            _login_log(f"登录成功，账号 {nid} 的凭据已保存")
            if nid != ACCOUNT_ID:
                _login_log(f"注意：网关固定读账号 {ACCOUNT_ID}，新账号 {nid} 的凭据不会被当前网关使用")
            return {"result": "connected", "account_id": nid}
        else:
            _login_log(f"未知登录状态 {status!r}，继续等待")
        sleep_fn(1)
    return fail("登录超时，请重试")
