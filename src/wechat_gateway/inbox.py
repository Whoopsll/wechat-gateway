"""消息标识、去重和入库。"""

import hashlib
import json
import threading
import time
from .media import message_content
from .storage import db, latest_ctx_token
from .typing_status import typing

def extract_text(item_list):
    for item in item_list or []:
        if item.get("type") == 1 and item.get("text_item", {}).get("text") is not None:
            return str(item["text_item"]["text"])
    return ""

def msg_key(msg):
    """稳定消息ID：优先微信 message_id；否则 发送者+正文+时间 的哈希（避免同一句话发两次被误吞）"""
    mid = str(msg.get("message_id") or "").strip()
    if mid:
        return mid
    h = hashlib.sha256()
    h.update(msg.get("from_user_id", "").encode())
    h.update(json.dumps(msg.get("item_list", []), sort_keys=True, ensure_ascii=False).encode())
    h.update(str(msg.get("create_time_ms") or msg.get("create_time") or "").encode())
    return "hash:" + h.hexdigest()[:16]


# ---------- 收消息 ----------
def on_wechat_message(msg):
    from_id = msg.get("from_user_id", "")
    if not msg.get("item_list"):
        return
    mid = msg_key(msg)
    if db().execute("SELECT 1 FROM inbox WHERE msg_id=?", (mid,)).fetchone():
        return  # 重复消息，不重复下载
    text = message_content(msg, mid)
    if not text:
        return
    ctx = msg.get("context_token") or ""
    now = time.time()
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute("INSERT OR IGNORE INTO inbox(msg_id,chat_id,content,created_at,ctx_token) "
                           "VALUES(?,?,?,?,?)", (mid, from_id, text, now, ctx))
        if cur.rowcount == 1:
            conn.execute("INSERT INTO history VALUES(?,?,?,?)", (from_id, "user", text, now))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if cur.rowcount != 1:
        return  # 重复消息，忽略
    print(f"[inbound] id={mid[:12]} from={from_id} body={text[:40]}", flush=True)
    threading.Thread(target=typing.start, args=(from_id, ctx or latest_ctx_token(from_id)),
                     daemon=True).start()
