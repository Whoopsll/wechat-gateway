"""文本发送、防重和批次回复收尾。"""

import http.client
import json
import sqlite3
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
import uuid
from .api import CurlTransportError, api_post, base_info, load_token
from .claims import heartbeat
from .storage import db, latest_ctx_token

# ---------- 发消息 ----------
class SendUncertain(Exception):
    """请求已经发出，但没拿到响应（响应途中断开/读超时）：微信很可能已经处理了，不能当失败重发"""

def _sendmessage(to_user_id, item, timeout=15):
    """调用 sendmessage，把网络错误分成两类：
    - 确定失败：curl 明确的请求前错误、HTTP 拒绝或 API 明确返回非零 ret，可重试。
    - 不确定：发送/接收中断、超时、HTTP 408/5xx、响应格式异常，保留防重记录。"""
    body = {"msg": {"from_user_id": "", "to_user_id": to_user_id,
                    "client_id": str(uuid.uuid4()), "message_type": 2, "message_state": 2,
                    "item_list": [item], "context_token": latest_ctx_token(to_user_id)},
            "base_info": base_info()}
    # 凭据读取失败发生在请求前，不应伪装成发送结果不确定。
    token = load_token()
    try:
        resp = api_post("/ilink/bot/sendmessage", body, token, timeout=timeout)
    except CurlTransportError as e:
        if not e.request_may_have_been_sent:
            raise
        raise SendUncertain(f"{type(e).__name__}: {e}") from e
    except urllib.error.HTTPError as e:
        if e.code != 408 and e.code < 500:
            raise
        raise SendUncertain(f"HTTP {e.code}: response does not confirm delivery") from e
    except (urllib.error.URLError, TimeoutError, ConnectionError,
            http.client.HTTPException, OSError, ValueError) as e:
        raise SendUncertain(f"{type(e).__name__}: {e}") from e
    if not isinstance(resp, dict):
        raise SendUncertain("sendmessage response is not an object")
    ret = resp.get("ret")
    # 沿用原协议兼容性：成功对象可省略 ret 或返回 null。
    if ret is not None and type(ret) is not int:
        raise SendUncertain("sendmessage response has invalid ret")
    if ret not in (0, None):
        raise RuntimeError(f"send failed ret={resp.get('ret')} {resp.get('errmsg', '')}")

def _post_text(to_user_id, text):
    _sendmessage(to_user_id, {"type": 1, "text_item": {"text": text}})

def _record_uncertain(chat_id, claim_id, what, err):
    db().execute("INSERT INTO events VALUES(?,?,?,?,?)",
                 (time.time(), "send_uncertain", chat_id, claim_id,
                  json.dumps({"what": what[:100], "error": str(err)[:200]}, ensure_ascii=False)))
    print(f"[uncertain] {what[:40]} {err}", file=sys.stderr, flush=True)

def send_text(to_user_id, text, reply_to, *, claim_id=None):
    """幂等发送；回复的代次校验、续期和防重占位在同一写事务中完成。"""
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if claim_id is not None:
            row = conn.execute("SELECT claim_id, chat_id, status FROM inbox WHERE msg_id=?",
                               (reply_to,)).fetchone()
            if not row or row[:2] != (claim_id, to_user_id) or row[2] not in ("processing", "replied"):
                conn.execute("COMMIT")
                return "superseded"
            if row[2] == "replied":
                conn.execute("COMMIT")
                return "already_replied"
            conn.execute("UPDATE inbox SET claimed_at=MAX(claimed_at, ?) "
                         "WHERE claim_id=? AND status='processing'", (time.time(), claim_id))
        cur = conn.execute("INSERT OR IGNORE INTO sent(reply_to,sent_at) VALUES(?,?)",
                           (reply_to, time.time()))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if not cur.rowcount:
        print(f"[skip] already replied to {reply_to[:12]}", flush=True)
        return "already_replied"
    try:
        _post_text(to_user_id, text)
    except SendUncertain as e:
        _record_uncertain(to_user_id, None, f"reply:{text}", e)   # 保留防重记录，不再重发
        return "uncertain"
    except Exception:
        db().execute("DELETE FROM sent WHERE reply_to=?", (reply_to,))
        raise
    print(f"[sent] to={to_user_id} len={len(text)}", flush=True)
    return "ok"

SAY_MAX_PER_CLAIM = 5   # 一次认领最多发几条中间消息，防止刷屏

def say(claim_id, text):
    """发一条中间消息（进度、分开说的话），不结束本批。
    ok         = 已发出，同时算一次心跳
    done / superseded = 这批已经不归你了，没有发送，立刻停下
    limit      = 本批中间消息已达上限，没有发送，请用 reply 收尾"""
    text = text.strip().replace('\\"', '"').replace("\\'", "'")
    if not text:
        raise ValueError("empty message")
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT chat_id FROM inbox WHERE claim_id=? AND status='processing' LIMIT 1",
                           (claim_id,)).fetchone()
        if not row:
            conn.execute("COMMIT")
            return heartbeat(claim_id)          # 返回 done 或 superseded
        chat_id = row[0]
        n = conn.execute("SELECT COUNT(*) FROM sent WHERE reply_to LIKE ?",
                         (f"say:{claim_id}:%",)).fetchone()[0]
        if n >= SAY_MAX_PER_CLAIM:
            conn.execute("COMMIT")
            return "limit"
        key = f"say:{claim_id}:{n}"
        conn.execute("INSERT INTO sent(reply_to,sent_at) VALUES(?,?)", (key, time.time()))
        conn.execute("UPDATE inbox SET claimed_at=MAX(claimed_at, ?) WHERE claim_id=? AND status='processing'",
                     (time.time(), claim_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    result = "ok"
    try:
        _post_text(chat_id, text)
    except SendUncertain as e:
        _record_uncertain(chat_id, claim_id, f"say:{text}", e)
        result = "uncertain"
    except Exception:
        db().execute("DELETE FROM sent WHERE reply_to=?", (key,))
        raise
    db().execute("INSERT INTO history VALUES(?,?,?,?)", (chat_id, "assistant", text, time.time()))
    print(f"[say] to={chat_id} len={len(text)} {result}", flush=True)
    return result


def send_reply(reply_to, chat_id, text, *, claim_id):
    """带原认领身份回复；过期身份返回 superseded，不发送也不结束其他批次。"""
    # worker 有时会多余地转义引号，微信里会显示成 \" ，这里还原
    text = text.strip().replace('\\"', '"').replace("\\'", "'")
    if not text:
        raise ValueError("empty reply")
    if not claim_id:
        raise ValueError("claim_id is required")
    result = send_text(chat_id, text, reply_to, claim_id=claim_id)
    if result == "superseded":
        return result
    # 始终用调用方提供的原 claim_id 收尾，不能查消息当前所属的代次。
    db().execute("UPDATE inbox SET status='replied' WHERE chat_id=? AND status='processing' "
                 "AND claim_id=?", (chat_id, claim_id))
    if result == "already_replied":
        return result
    db().execute("INSERT INTO history VALUES(?,?,?,?)", (chat_id, "assistant", text, time.time()))
    return result   # ok 或 uncertain（很可能已送达，同样算结束）
