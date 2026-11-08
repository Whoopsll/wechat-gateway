"""提醒创建、主动通知和到期派发。"""

import datetime
import json
import os
import re
import sqlite3
import time
import uuid
from .config import WATCH_DIR
from .messages import SendUncertain, _post_text, _record_uncertain
from .storage import db

# ---------- 定时提醒 ----------
# 动机：用户在微信里说"10 分钟后提醒我喝水"，worker 不能只口头答应，必须落成真机制。
# 机制：worker 调 `remind` 把提醒写成规格文件（reminders/pending/<uuid>.json）；
# 主侧每分钟跑 `dispatch_reminders` 派发到期规格；派发经 `notify` 主动发微信。
# 全都不依赖 worker 自己建 cron，也不依赖 hook 认领。
REMINDERS_DIR = os.path.join(WATCH_DIR, "reminders")   # 测试可整体覆盖
REMIND_DELAY_MIN = 30            # --delay-seconds 下限
REMIND_DELAY_MAX = 7 * 86400    # --delay-seconds 上限（一周）
REMIND_MAX_ATTEMPTS = 5         # 派发连续失败这么多次后移到 failed/
_REMIND_BEIJING = datetime.timezone(datetime.timedelta(hours=8))

def _beijing_str(epoch):
    """epoch → 北京时间 "YYYY-MM-DD HH:MM"（本机是 UTC 时区，不能直接用 localtime）"""
    return datetime.datetime.fromtimestamp(epoch, tz=_REMIND_BEIJING).strftime("%Y-%m-%d %H:%M")

def _parse_at(s):
    """--at 参数：纯数字当 epoch 秒；否则按北京时间 "YYYY-MM-DD HH:MM" 解析。返回 epoch（float）"""
    s = s.strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    try:
        dt = datetime.datetime.strptime(s, "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError('--at 格式应为 epoch 秒或 "YYYY-MM-DD HH:MM"（北京时间）')
    return dt.replace(tzinfo=_REMIND_BEIJING).timestamp()

def chat_known(chat_id):
    """chat_id 在 wechat.db 里有既往会话（inbox 或 history 里出现过）"""
    row = db().execute(
        "SELECT 1 FROM inbox WHERE chat_id=? UNION SELECT 1 FROM history WHERE chat_id=? LIMIT 1",
        (chat_id, chat_id)).fetchone()
    return bool(row)

def notify(chat_id, text, key=None):
    """主动向 chat_id 发一条文本（无需认领，用于提醒到点派发）。
    成功返回 "ok"；带 key 且该 key 已发过返回 "duplicate"（不重发）；SendUncertain
    按现有发送语义保留防重记录、返回 "uncertain"。chat_id 无既往会话或文本为空抛
    ValueError；其他发送失败抛异常（sent 记录已撤销，调用方可重试）"""
    text = text.strip().replace('\\"', '"').replace("\\'", "'")
    if not text:
        raise ValueError("empty text")
    if not chat_known(chat_id):
        raise ValueError(f"unknown chat_id: {chat_id[:24]}")
    rec_id = f"notify:{key}" if key else f"notify:{uuid.uuid4().hex}"
    try:
        db().execute("INSERT INTO sent(reply_to,sent_at) VALUES(?,?)", (rec_id, time.time()))
    except sqlite3.IntegrityError:
        print(f"[skip] duplicate notify key={key}", flush=True)
        return "duplicate"
    try:
        _post_text(chat_id, text)
    except SendUncertain as e:
        _record_uncertain(chat_id, None, f"notify:{text}", e)   # 保留防重记录，不再重发
        db().execute("INSERT INTO history VALUES(?,?,?,?)", (chat_id, "assistant", text, time.time()))
        return "uncertain"
    except Exception:
        db().execute("DELETE FROM sent WHERE reply_to=?", (rec_id,))
        raise
    print(f"[sent] notify to={chat_id[:24]} len={len(text)}", flush=True)
    db().execute("INSERT INTO history VALUES(?,?,?,?)", (chat_id, "assistant", text, time.time()))
    return "ok"

def remind_create(chat_id, text, delay_seconds=None, at=None):
    """创建提醒规格，原子落盘到 reminders/pending/<uuid>.json。
    --at 与 --delay-seconds 同时给时用 --at；都不给抛 ValueError。
    校验失败抛 ValueError。返回规格 dict"""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty text")
    if not chat_known(chat_id):
        raise ValueError(f"unknown chat_id: {chat_id[:24]}")
    now = time.time()
    if at is not None:
        due = _parse_at(at)
    elif delay_seconds is not None:
        d = int(delay_seconds)
        if not (REMIND_DELAY_MIN <= d <= REMIND_DELAY_MAX):
            raise ValueError(f"--delay-seconds 必须在 {REMIND_DELAY_MIN}..{REMIND_DELAY_MAX}")
        due = now + d
    else:
        raise ValueError("必须给 --delay-seconds 或 --at 之一")
    if due < now - 30:
        raise ValueError("--at 指定的时间已过去")
    rid = uuid.uuid4().hex
    spec = {"id": rid, "chat_id": chat_id, "due_epoch": due, "text": text,
            "created_at": now, "attempts": 0}
    pd = os.path.join(REMINDERS_DIR, "pending")
    for sub in ("pending", "done", "failed"):
        os.makedirs(os.path.join(REMINDERS_DIR, sub), exist_ok=True)
    tmp = os.path.join(pd, f".{rid}.tmp")
    with open(tmp, "w") as f:
        json.dump(spec, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(pd, f"{rid}.json"))
    return spec

def _remind_move(spec, dest):
    """把规格文件原子移到 done/ 或 failed/"""
    src = os.path.join(REMINDERS_DIR, "pending", spec["id"] + ".json")
    os.makedirs(os.path.join(REMINDERS_DIR, dest), exist_ok=True)
    os.replace(src, os.path.join(REMINDERS_DIR, dest, spec["id"] + ".json"))

def _remind_log(msg):
    """派发日志：一行一条，北京时间戳"""
    try:
        line = f"{_beijing_str(time.time())} {msg}\n"
        with open(os.path.join(REMINDERS_DIR, "dispatch.log"), "a") as f:
            f.write(line)
    except OSError:
        pass

def dispatch_reminders(now_fn=None, notify_fn=None):
    """派发器：主侧每分钟跑一次。到期规格经 notify 发出并移到 done/；
    失败的 attempts+1 留在 pending，>=REMIND_MAX_ATTEMPTS 的移到 failed/。
    返回 {sent, failed, pending}。任何单条失败都不抛异常（退出码恒 0，由调用方
    打印摘要）；拿不到锁（另一轮派发在跑）时本轮跳过。
    now_fn / notify_fn 可注入，供单元测试用。"""
    now_fn = now_fn or time.time
    notify_fn = notify_fn or (lambda cid, text, key: notify(cid, text, key))
    summary = {"sent": 0, "failed": 0, "pending": 0}
    pd = os.path.join(REMINDERS_DIR, "pending")
    os.makedirs(pd, exist_ok=True)
    import fcntl
    fd = open(os.path.join(REMINDERS_DIR, "dispatch.lock"), "a+")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fd.close()
        _remind_log("skipped: locked")
        return summary
    try:
        now = now_fn()
        for name in sorted(os.listdir(pd)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(pd, name)
            try:
                with open(path) as f:
                    spec = json.load(f)
            except Exception as e:
                _remind_log(f"bad spec file {name}: {e}")
                continue
            if spec.get("due_epoch", 0) > now:
                summary["pending"] += 1
                continue
            try:
                r = notify_fn(spec["chat_id"], spec["text"], spec["id"])
                _remind_move(spec, "done")
                _remind_log(f"{spec['id']} sent ({r})")
                summary["sent"] += 1
            except Exception as e:
                spec["attempts"] = int(spec.get("attempts", 0)) + 1
                spec["last_error"] = str(e)[:200]
                if spec["attempts"] >= REMIND_MAX_ATTEMPTS:
                    _remind_move(spec, "failed")
                    _remind_log(f"{spec['id']} failed x{spec['attempts']} -> failed/ ({spec['last_error']})")
                else:
                    tmp = path + ".tmp"
                    with open(tmp, "w") as f:
                        json.dump(spec, f, ensure_ascii=False)
                    os.replace(tmp, path)
                    _remind_log(f"{spec['id']} failed x{spec['attempts']}, retry later ({spec['last_error']})")
                summary["failed"] += 1
    finally:
        fd.close()
    return summary
