"""图片压缩、CDN 上传和文件发送。"""

import base64
import hashlib
import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from .api import api_post, base_info, load_token
from .claims import heartbeat
from .config import CDN_BASE, IMAGE_EXTS, MEDIA_DIR, MEDIA_RETRY_SLEEP, SEND_FILE_MAX
from .media import _aes_ecb, _cdn_upload
from .messages import SendUncertain, _record_uncertain, _sendmessage
from .storage import db

# ---------- 发文件/图片 ----------
IMAGE_COMPRESS_OVER = 1024 * 1024   # 超过 1MB 的图片发送前压成 JPEG（有 Pillow 才做）
IMAGE_MAX_SIDE = 2048

def _prepare_image(path):
    """大图压成 JPEG（最长边 2048、质量 85），返回实际要上传的路径。没装 Pillow 或压缩没变小就用原图"""
    if os.path.getsize(path) <= IMAGE_COMPRESS_OVER:
        return path
    try:
        from PIL import Image
    except ImportError:
        return path
    try:
        img = Image.open(path)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")
        img.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
        out_dir = os.path.join(MEDIA_DIR, "outgoing")
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, os.path.splitext(os.path.basename(path))[0] + ".jpg")
        img.save(out, "JPEG", quality=85, optimize=True)
        if os.path.getsize(out) < os.path.getsize(path):
            print(f"[send_file] 图片压缩 {os.path.getsize(path)} → {os.path.getsize(out)} 字节",
                  file=sys.stderr, flush=True)
            return out
    except Exception as e:
        print(f"[send_file] 压缩失败，用原图: {e}", file=sys.stderr, flush=True)
    return path

UPLOAD_SAFE_BYTES = 120 * 1024   # 微信 CDN 大于约 128KB 时常断；上传失败后把图压到这个大小以内再试

def _shrink_image(path, max_bytes=UPLOAD_SAFE_BYTES):
    """把图片压到 max_bytes 以内，返回新路径；没有 Pillow 或怎么压都压不下来返回 None"""
    try:
        from PIL import Image
    except ImportError:
        return None
    img = Image.open(path)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    out_dir = os.path.join(MEDIA_DIR, "outgoing")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, os.path.splitext(os.path.basename(path))[0] + "_small.jpg")
    for side in (1600, 1280, 1024, 800, 640, 480):
        for q in (82, 72, 62):
            im = img.copy()
            im.thumbnail((side, side))
            im.save(out, "JPEG", quality=q, optimize=True)
            if os.path.getsize(out) <= max_bytes:
                print(f"[send_file] 压到 {side}px q{q}: {os.path.getsize(out)} 字节", file=sys.stderr, flush=True)
                return out
    return None

def upload_media(path, to_user_id, media_type):
    """加密上传到微信 CDN，返回 (CDNMedia dict, 原始大小, 密文大小, md5)"""
    with open(path, "rb") as f:
        raw = f.read()
    aeskey = os.urandom(16)
    cipher = _aes_ecb(aeskey, raw, encrypt=True)
    filekey = os.urandom(16).hex()
    md5 = hashlib.md5(raw).hexdigest()
    t0 = time.time()
    _log = lambda m: print(f"[send_file] {m} ({time.time() - t0:.1f}s)", file=sys.stderr, flush=True)
    _log("getuploadurl ...")
    resp = api_post("/ilink/bot/getuploadurl", {
        "filekey": filekey, "media_type": media_type, "to_user_id": to_user_id,
        "rawsize": len(raw), "rawfilemd5": md5, "filesize": len(cipher),
        "no_need_thumb": True, "aeskey": aeskey.hex(), "base_info": base_info(),
    }, load_token(), timeout=15)
    # 实测返回的是完整上传地址 upload_full_url；社区文档写的是 upload_param，两种都兼容
    if resp.get("ret") not in (0, None) or not (resp.get("upload_full_url") or resp.get("upload_param")):
        raise RuntimeError(f"getuploadurl failed ret={resp.get('ret')} {resp.get('errmsg', '')} "
                           f"fields={sorted(resp)}")
    if resp.get("upload_full_url"):
        url = resp["upload_full_url"]
    else:
        url = (f"{CDN_BASE}/upload?encrypted_query_param={urllib.parse.quote(resp['upload_param'], safe='')}"
               f"&filekey={filekey}")
    _log("cdn upload ...")
    param = _cdn_upload(url, cipher)
    _log("cdn upload ok")
    media = {"encrypt_query_param": param,
             "aes_key": base64.b64encode(aeskey.hex().encode()).decode(),   # 注意：是对 hex 字符串做 base64
             "encrypt_type": 1}
    return media, len(raw), len(cipher), md5

FILE_UNCERTAIN_RESENDS = 1   # 图片/文件 sendmessage 结果不确定时，用同一份已上传的媒体补发几次

def _send_media_item(chat_id, claim_id, item, name):
    """发送媒体消息。和文字不同：丢图比重复一张图糟糕得多，所以结果不确定时补发。
    （22:40 实测：微信不按 client_id 去重，补发可能导致收到两张，这是有意接受的代价）
    返回 ok（某次明确成功）/ uncertain（全部不确定）；首次就明确失败则抛出"""
    try:
        _sendmessage(chat_id, item, timeout=30)
        return "ok"
    except SendUncertain as e:
        _record_uncertain(chat_id, claim_id, f"file:{name}", e)
    for i in range(FILE_UNCERTAIN_RESENDS):
        time.sleep(MEDIA_RETRY_SLEEP)
        print(f"[send_file] 结果不确定，补发第 {i + 1} 次", file=sys.stderr, flush=True)
        try:
            _sendmessage(chat_id, item, timeout=30)   # 新 client_id；媒体已在 CDN 上，不用重新上传
            return "ok"
        except SendUncertain as e:
            _record_uncertain(chat_id, claim_id, f"file:{name}(补发{i + 1})", e)
        except Exception as e:
            # 第一次可能已经到了，不能当明确失败
            _record_uncertain(chat_id, claim_id, f"file:{name}(补发{i + 1}失败)", e)
    return "uncertain"

def send_file(claim_id, path):
    """把本地文件/图片直接发到微信。图片按图片发，其他按文件发。防重 key 绑在消息上。
    ok / uncertain（网络中断且补发也不确定，要请用户确认有没有收到）/ already_sent /
    too_large（超过 20MB，请改用 send_mail）/ done / superseded"""
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    if os.path.getsize(path) > SEND_FILE_MAX:
        return "too_large"
    name = os.path.basename(path)
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute("SELECT msg_id, chat_id FROM inbox WHERE claim_id=? AND status='processing'",
                            (claim_id,)).fetchall()
        if not rows:
            conn.execute("COMMIT")
            return heartbeat(claim_id)
        chat_id = rows[0][1]
        keys = [f"file:{m}:{name}" for m, _ in rows]
        if conn.execute("SELECT 1 FROM sent WHERE reply_to IN (%s) LIMIT 1" % ",".join("?" * len(keys)),
                        keys).fetchone():
            conn.execute("COMMIT")
            return "already_sent"
        conn.executemany("INSERT INTO sent(reply_to,sent_at) VALUES(?,?)", [(k, time.time()) for k in keys])
        conn.execute("UPDATE inbox SET claimed_at=MAX(claimed_at, ?) WHERE claim_id=? AND status='processing'",
                     (time.time(), claim_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    stage = "prepare"
    try:
        is_image = os.path.splitext(name)[1].lower() in IMAGE_EXTS
        upload_path = _prepare_image(path) if is_image else path
        stage = "upload"
        try:
            media, rawsize, ciphersize, md5 = upload_media(upload_path, chat_id, 1 if is_image else 3)
        except Exception as first_err:
            small = (_shrink_image(upload_path)
                     if is_image and os.path.getsize(upload_path) > UPLOAD_SAFE_BYTES else None)
            if not small:
                raise
            print(f"[send_file] 上传失败（{first_err}），压缩后重试", file=sys.stderr, flush=True)
            stage = "upload(压缩后重试)"
            media, rawsize, ciphersize, md5 = upload_media(small, chat_id, 1)
        stage = "sendmessage"
        if is_image:
            item = {"type": 2, "image_item": {"media": media, "mid_size": ciphersize}}
        else:
            item = {"type": 4, "file_item": {"media": media, "file_name": name,
                                             "md5": md5, "len": str(rawsize)}}
        print("[send_file] sendmessage ...", file=sys.stderr, flush=True)
        result = _send_media_item(chat_id, claim_id, item, name)
    except Exception as e:
        db().executemany("DELETE FROM sent WHERE reply_to=?", [(k,) for k in keys])
        # 报错写进 events：worker 的输出网关看不到，靠这里留底
        db().execute("INSERT INTO events VALUES(?,?,?,?,?)",
                     (time.time(), "send_file_failed", chat_id, claim_id,
                      json.dumps({"file": name, "size": os.path.getsize(path), "stage": stage,
                                  "error": f"{type(e).__name__}: {e}"[:500]}, ensure_ascii=False)))
        raise
    kind = "图片" if is_image else "文件"
    note = f"[已发{kind} {name}]" if result == "ok" else f"[{kind} {name} 发送时网络中断，不确定对方是否收到]"
    db().execute("INSERT INTO history VALUES(?,?,?,?)", (chat_id, "assistant", note, time.time()))
    print(f"[file] to={chat_id} name={name} size={rawsize} {result}", flush=True)
    return result
