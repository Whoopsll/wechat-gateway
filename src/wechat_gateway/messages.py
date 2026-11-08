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
from .api import api_post, base_info, load_token
from .claims import heartbeat
from .storage import db, latest_ctx_token

# ---------- 发消息 ----------
class SendUncertain(Exception):
    """请求已经发出，但没拿到响应（响应途中断开/读超时）：微信很可能已经处理了，不能当失败重发"""

def _sendmessage(to_user_id, item, timeout=15):
    """调用 sendmessage，把网络错误分成两类：
    - 确定没发出：连接阶段失败（URLError）、服务器明确拒绝（HTTPError、ret!=0）→ 原样抛出，可以重试
    - 不确定：请求已发出，读响应时断开/超时 → 抛 SendUncertain，调用方不能重发"""
    body = {"msg": {"from_user_id": "", "to_user_id": to_user_id,
                    "client_id": str(uuid.uuid4()), "message_type": 2, "message_state": 2,
                    "item_list": [item], "context_token": latest_ctx_token(to_user_id)},
            "base_info": base_info()}
    try:
        resp = api_post("/ilink/bot/sendmessage", body, load_token(), timeout=timeout)
    except urllib.error.URLError:
        raise                       # 含 HTTPError；urllib 只把"发请求阶段"的错误包成 URLError
    except (TimeoutError, ConnectionError, http.client.HTTPException, OSError) as e:
        raise SendUncertain(f"{type(e).__name__}: {e}") from e
    if resp.get("ret") not in (0, None):
        raise RuntimeError(f"send failed ret={resp.get('ret')} {resp.get('errmsg', '')}")

def _post_text(to_user_id, text):
    _sendmessage(to_user_id, {"type": 1, "text_item": {"text": text}})

def _record_uncertain(chat_id, claim_id, what, err):
    db().execute("INSERT INTO events VALUES(?,?,?,?,?)",
                 (time.time(), "send_uncertain", chat_id, claim_id,
                  json.dumps({"what": what[:100], "error": str(err)[:200]}, ensure_ascii=False)))
    print(f"[uncertain] {what[:40]} {err}", file=sys.stderr, flush=True)

def send_text(to_user_id, text, reply_to):
    """幂等发送：同一个 reply_to 最多成功发一次；失败会撤销记录以便重试"""
    try:
        db().execute("INSERT INTO sent(reply_to,sent_at) VALUES(?,?)", (reply_to, time.time()))
    except sqlite3.IntegrityError:
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


def send_reply(reply_to, chat_id, text):
    """成功返回 ok，重复返回 already_replied，失败抛异常（消息仍是 processing，超时后自动重试）"""
    # worker 有时会多余地转义引号，微信里会显示成 \" ，这里还原
    text = text.strip().replace('\\"', '"').replace("\\'", "'")
    if not text:
        raise ValueError("empty reply")
    result = send_text(chat_id, text, reply_to)
    # 只收尾本批（和 reply_to 同一次认领的消息），不碰之后排队的新消息
    # already_replied 也要收尾：可能是上次发出去了但没来得及改状态
    db().execute("UPDATE inbox SET status='replied' WHERE chat_id=? AND status='processing' "
                 "AND claim_id=(SELECT claim_id FROM inbox WHERE msg_id=?)", (chat_id, reply_to))
    if result == "already_replied":
        return result
    db().execute("INSERT INTO history VALUES(?,?,?,?)", (chat_id, "assistant", text, time.time()))
    return result   # ok 或 uncertain（很可能已送达，同样算结束）
