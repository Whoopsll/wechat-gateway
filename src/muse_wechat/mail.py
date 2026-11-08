"""通过原运行环境的邮件 CLI 发送附件。"""

import json
import os
import subprocess
import time
from .claims import heartbeat
from .config import GWS_CLI, MAIL_ALLOWED, MAIL_MAX_BYTES, MAIL_TIMEOUT
from .storage import db

# ---------- 邮件 ----------
def _run_mail_cli(args, timeout):
    """真正调用 Gmail CLI；测试里会替换掉"""
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

def send_mail(claim_id, to, subject, body, attachments=(), html=False):
    """幂等发邮件。防重 key 绑在消息上（mail:<msg_id>:<收件人>），回收重做换了 claim_id 也拦得住。
    ok            = 已发出
    already_sent  = 这批消息之前已经给这些收件人发过了，没有重发
    not_allowed   = 收件人不在白名单，没有发送
    timeout       = CLI 超时，不确定是否已发出，不要重试，用 reply 告诉用户去邮箱确认
    done / superseded = 这批已经不归你了，没有发送，立刻停下"""
    rcpts = sorted({r.strip().lower() for r in to.split(",") if r.strip()})
    if not rcpts:
        raise ValueError("no recipient")
    bad = [r for r in rcpts if r not in MAIL_ALLOWED]
    if bad:
        return "not_allowed"
    for f in attachments:
        if not os.path.isfile(f):
            raise FileNotFoundError(f)
    if sum(os.path.getsize(f) for f in attachments) > MAIL_MAX_BYTES:
        raise ValueError("attachments exceed 25MB")

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute("SELECT msg_id, chat_id FROM inbox WHERE claim_id=? AND status='processing'",
                            (claim_id,)).fetchall()
        if not rows:
            conn.execute("COMMIT")
            return heartbeat(claim_id)
        chat_id = rows[0][1]
        msg_ids = [r[0] for r in rows]
        todo = [r for r in rcpts
                if not conn.execute("SELECT 1 FROM sent WHERE reply_to IN (%s) LIMIT 1"
                                    % ",".join("?" * len(msg_ids)),
                                    [f"mail:{m}:{r}" for m in msg_ids]).fetchone()]
        if not todo:
            conn.execute("COMMIT")
            return "already_sent"
        keys = [f"mail:{m}:{r}" for m in msg_ids for r in todo]
        conn.executemany("INSERT OR IGNORE INTO sent(reply_to,sent_at) VALUES(?,?)",
                         [(k, time.time()) for k in keys])
        conn.execute("UPDATE inbox SET claimed_at=MAX(claimed_at, ?) WHERE claim_id=? AND status='processing'",
                     (time.time(), claim_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    args = [GWS_CLI, "gmail", "+send", "--to", ",".join(todo), "--subject", subject, "--body", body]
    for f in attachments:
        args += ["-a", f]
    if html:
        args.append("--html")
    names = "、".join(os.path.basename(f) for f in attachments)
    try:
        r = _run_mail_cli(args, MAIL_TIMEOUT)
    except subprocess.TimeoutExpired:
        # 可能已经发出去了：保留防重记录，不让重做再发一次
        db().execute("INSERT INTO events VALUES(?,?,?,?,?)",
                     (time.time(), "mail_timeout", chat_id, claim_id,
                      json.dumps({"to": todo, "subject": subject}, ensure_ascii=False)))
        db().execute("INSERT INTO history VALUES(?,?,?,?)",
                     (chat_id, "assistant", f"[邮件发送超时，可能已发出：{subject}]", time.time()))
        return "timeout"
    except Exception:
        db().executemany("DELETE FROM sent WHERE reply_to=?", [(k,) for k in keys])
        raise
    if r.returncode != 0:
        db().executemany("DELETE FROM sent WHERE reply_to=?", [(k,) for k in keys])
        raise RuntimeError(f"mail failed rc={r.returncode} {r.stderr.strip()[:300]}")
    note = f"[已发邮件给 {','.join(todo)}：{subject}" + (f"，附件 {names}" if names else "") + "]"
    db().execute("INSERT INTO history VALUES(?,?,?,?)", (chat_id, "assistant", note, time.time()))
    print(f"[mail] to={todo} subject={subject[:30]}", flush=True)
    return "ok"
