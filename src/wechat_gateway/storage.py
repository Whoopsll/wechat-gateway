"""SQLite 存储、同步游标和会话上下文。"""

import json
import os
import sqlite3
import tempfile
import threading
from .config import CTX_TOKEN_FILE, DB_PATH, SYNC_FILE

# ---------- DB（每个线程一个连接） ----------
_local = threading.local()

def db():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS inbox(
            msg_id TEXT PRIMARY KEY, chat_id TEXT, content TEXT,
            status TEXT DEFAULT 'pending', claimed_at REAL, created_at REAL)""")
        for col in ("ctx_token TEXT", "claim_id TEXT"):
            try:
                conn.execute(f"ALTER TABLE inbox ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # 列已存在
        conn.execute("""CREATE TABLE IF NOT EXISTS sent(
            reply_to TEXT PRIMARY KEY, sent_at REAL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS history(
            chat_id TEXT, role TEXT, content TEXT, ts REAL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS events(
            ts REAL, kind TEXT, chat_id TEXT, claim_id TEXT, detail TEXT)""")
        _local.conn = conn
    return conn


def get_updates_buf():
    try:
        with open(SYNC_FILE) as f:
            return json.load(f).get("get_updates_buf", "")
    except Exception:
        return ""

def save_updates_buf(buf):
    """原子替换游标文件，写入失败时保留原文件。"""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=os.path.dirname(SYNC_FILE),
                                         prefix=".sync-", suffix=".tmp", delete=False) as f:
            tmp = f.name
            json.dump({"get_updates_buf": buf}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SYNC_FILE)
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)

def file_ctx_token(user_id):
    """旧 OpenClaw 留下的 context_token，作兜底"""
    try:
        with open(CTX_TOKEN_FILE) as f:
            return json.load(f).get(user_id, "")
    except Exception:
        return ""

def latest_ctx_token(chat_id):
    row = db().execute("SELECT ctx_token FROM inbox WHERE chat_id=? AND ctx_token IS NOT NULL "
                       "AND ctx_token != '' ORDER BY created_at DESC LIMIT 1", (chat_id,)).fetchone()
    return row[0] if row else file_ctx_token(chat_id)
