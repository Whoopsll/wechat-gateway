"""任务认领、续期、回收和段落守窗。"""

import json
import os
import sys
import time
import uuid
from .config import HISTORY_N, STALE_SECS, WATCH_DIR
from .storage import db

# ---------- 异常事件 ----------
def _record_reclaims(conn, now):
    """回收前把每个超时批次记进 events：要么 worker 挂了，要么绕过网关发了消息没收尾"""
    stale = conn.execute("SELECT claim_id, chat_id, group_concat(content, ' / '), "
                         "MIN(created_at), MAX(claimed_at) FROM inbox "
                         "WHERE status='processing' AND claimed_at < ? GROUP BY claim_id",
                         (now - STALE_SECS,)).fetchall()
    for claim_id, chat_id, contents, created, last_beat in stale:
        said = conn.execute("SELECT COUNT(*) FROM sent WHERE reply_to LIKE ?",
                            (f"say:{claim_id}:%",)).fetchone()[0] if claim_id else 0
        detail = json.dumps({
            "messages": (contents or "")[:200],
            "silent_secs": int(now - last_beat) if last_beat else None,
            "since_received_secs": int(now - created) if created else None,
            "say_sent": said,   # >0 说明 worker 说过话却没 reply 收尾，多半是绕过或中途挂了
        }, ensure_ascii=False)
        conn.execute("INSERT INTO events VALUES(?,?,?,?,?)", (now, "reclaim", chat_id, claim_id, detail))
        print(f"[reclaim] chat={chat_id} claim={claim_id} {detail}", file=sys.stderr)

def list_events(n=20):
    rows = db().execute("SELECT ts, kind, chat_id, claim_id, detail FROM events "
                        "ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
    if not rows:
        return "没有异常事件"
    return "\n".join(f"{time.strftime('%m-%d %H:%M:%S', time.localtime(ts))}  {kind}  "
                     f"chat={chat_id}  claim={claim_id}  {detail}"
                     for ts, kind, chat_id, claim_id, detail in rows)

# ---------- 认领 ----------
def claim_batch():
    """认领最早有 pending 的那个人的全部 pending 消息，附最近上下文；没有返回 None"""
    now = time.time()
    conn = db()
    conn.execute("BEGIN IMMEDIATE")   # 认领过程整体加写锁，多个 claim 进程也不会抢到同一批
    try:
        _record_reclaims(conn, now)
        conn.execute("UPDATE inbox SET status='pending', claimed_at=NULL, claim_id=NULL "
                     "WHERE status='processing' AND claimed_at < ?", (now - STALE_SECS,))
        # 同一个聊天串行：还有消息在处理中的聊天，新消息先排队，等上一条回完再认领
        row = conn.execute("SELECT chat_id FROM inbox i WHERE status='pending' AND NOT EXISTS ("
                           "  SELECT 1 FROM inbox p WHERE p.chat_id=i.chat_id AND p.status='processing') "
                           "ORDER BY created_at LIMIT 1").fetchone()
        if not row:
            conn.execute("COMMIT")
            return None
        chat_id = row[0]
        claim_id = uuid.uuid4().hex[:12]
        conn.execute("UPDATE inbox SET status='processing', claimed_at=?, claim_id=? "
                     "WHERE chat_id=? AND status='pending'", (now, claim_id, chat_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    msgs = conn.execute("SELECT msg_id, content, created_at FROM inbox "
                        "WHERE claim_id=? ORDER BY created_at", (claim_id,)).fetchall()
    if not msgs:
        return None
    # 上下文 = 认领时刻之前的全部历史（含上一轮刚发出的回复），排除本批要回的消息
    batch_ts = {m[2] for m in msgs}
    rows = conn.execute("SELECT role, content, ts FROM history WHERE chat_id=? AND ts <= ? "
                        "ORDER BY ts DESC LIMIT ?", (chat_id, now, HISTORY_N + len(msgs))).fetchall()
    recent = [(r, c) for r, c, ts in rows if not (r == "user" and ts in batch_ts)][:HISTORY_N][::-1]
    return {
        "chat_id": chat_id,
        "claim_id": claim_id,      # 这次认领的身份，心跳用它判断自己有没有被顶替
        "reply_to": msgs[-1][0],   # 用这批最后一条的 id 做幂等键
        "messages": [m[1] for m in msgs],
        "recent": [{"role": r, "content": c} for r, c in recent],
    }

def heartbeat(claim_id):
    """长任务心跳：续期本次认领。
    ok         = 续期成功，继续干
    done       = 这批已经回复过了，停下
    superseded = 已超时被回收/被别的 worker 接手，停下，不要再发任何东西"""
    cur = db().execute("UPDATE inbox SET claimed_at=MAX(claimed_at, ?) WHERE claim_id=? AND status='processing'",
                       (time.time(), claim_id))
    if cur.rowcount:
        return "ok"
    row = db().execute("SELECT 1 FROM inbox WHERE claim_id=? AND status='replied' LIMIT 1",
                       (claim_id,)).fetchone()
    return "done" if row else "superseded"

HOLD_MAX_SECS = 600   # hold 最长能预约多久

def hold(claim_id, seconds):
    """worker 调用内置工具（生成图片、搜索等，没法用 run 包、期间也没法心跳）之前，
    预约一段时间：这段时间内不会被超时回收。到期还没动静照样回收。
    实现：把 claimed_at 推到未来，使"claimed_at + STALE_SECS"= 现在 + seconds。只延长、不缩短。
    返回 ok / done / superseded"""
    seconds = max(0, min(int(seconds), HOLD_MAX_SECS))
    target = time.time() + seconds - STALE_SECS
    cur = db().execute("UPDATE inbox SET claimed_at=MAX(claimed_at, ?) WHERE claim_id=? AND status='processing'",
                       (target, claim_id))
    if cur.rowcount:
        return "ok"
    return heartbeat(claim_id)

def unclaim(chat_id):
    """手动工具（worker 不调用）：把某个聊天正在处理的消息放回队列，跳过 2 分钟超时立即重试"""
    db().execute("UPDATE inbox SET status='pending', claimed_at=NULL, claim_id=NULL "
                 "WHERE chat_id=? AND status='processing'", (chat_id,))

# ---------- 段落守窗 ----------
WAIT_SESSION_FILE = os.path.join(WATCH_DIR, "wait-session.json")
WAIT_SEGMENT_MAX_SECS = 1800   # 一个聊天段落最长守这么久（30 分钟），超了强制收段
WAIT_SECONDS_MAX = 120         # 单次守窗的秒数上限

def _wait_session_load():
    """读段落计时记录；文件缺失/损坏一律当作没有记录"""
    try:
        with open(WAIT_SESSION_FILE) as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("started_at"), (int, float)):
            return d
    except Exception:
        pass
    return None

def _wait_session_save(session_id, started_at):
    tmp = WAIT_SESSION_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"session_id": session_id, "started_at": started_at}, f)
    os.replace(tmp, WAIT_SESSION_FILE)

def _wait_session_clear(session_id):
    """只清属于本段的记录，别人的段落计时不碰"""
    d = _wait_session_load()
    if d and d.get("session_id") == session_id:
        try:
            os.unlink(WAIT_SESSION_FILE)
        except OSError:
            pass

def wait_claim(session_id, seconds, claim_fn=None, now_fn=None, sleep_fn=None):
    """段落守窗：worker 回完一批后不结束，原地等同一聊天段落的下一批消息。
    - session_id 用段落首批的 claim_id，同一轮 worker 全程不变。段落计时从
      第一次见到这个 session_id 起算，整段封顶 WAIT_SEGMENT_MAX_SECS：超了
      返回 session_expired 且绝不认领（worker 必须结束本轮）。
    - 每秒走一次 claim 的原子认领路径：领到就原样返回那批（结构与 claim
      输出一致，含新 claim_id）；领不到（含被 hook 抢先认领的竞态）继续等，
      到时返回 timeout。timeout / session_expired 都清掉本段的计时记录。
    claim_fn / now_fn / sleep_fn 可注入，供单元测试用假认领和假时钟。"""
    claim_fn = claim_fn or claim_batch
    now_fn = now_fn or time.time
    sleep_fn = sleep_fn or time.sleep
    seconds = max(1, min(int(seconds), WAIT_SECONDS_MAX))
    rec = _wait_session_load()
    if rec and rec.get("session_id") == session_id:
        started_at = rec["started_at"]
    else:
        started_at = now_fn()
        _wait_session_save(session_id, started_at)
    deadline = now_fn() + seconds
    while True:
        now = now_fn()
        if now - started_at > WAIT_SEGMENT_MAX_SECS:
            _wait_session_clear(session_id)
            return {"messages": [], "session_expired": True}
        batch = claim_fn()
        if batch:
            return batch
        if now >= deadline:
            _wait_session_clear(session_id)
            return {"messages": [], "timeout": True}
        sleep_fn(1)
