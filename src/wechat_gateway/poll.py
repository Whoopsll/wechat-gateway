"""网关单实例锁和长轮询主循环。"""

import os
import sys
import threading
import time
from .api import api_post, base_info, load_token
from .config import WATCH_DIR
from .inbox import on_wechat_message
from .storage import get_updates_buf, save_updates_buf
from .typing_status import typing

# ---------- 主循环 ----------
LOCK_PATH = os.path.join(WATCH_DIR, "gateway.lock")
_lock_fd = None

def acquire_single_instance_lock():
    """同一时间只允许一个 poll 进程。拿不到锁返回 False（已有网关在跑）。
    用 flock：进程退出（包括崩溃、kill -9）时内核自动释放，不会留下死锁"""
    global _lock_fd
    import fcntl
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    fd = open(LOCK_PATH, "a+")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fd.close()
        return False
    fd.seek(0); fd.truncate(); fd.write(str(os.getpid())); fd.flush()
    _lock_fd = fd
    return True

def poll_loop():
    if not acquire_single_instance_lock():
        print(f"[gateway] 已有网关在运行（见 {LOCK_PATH}），本进程退出", flush=True)
        sys.exit(0)
    buf = get_updates_buf()
    threading.Thread(target=typing.run, daemon=True).start()
    print("[gateway] 启动：长轮询 + SQLite 去重 + 输入状态保活", flush=True)
    while True:
        try:
            # 同一账号重登后下一轮直接采用新凭据；读取失败也进入重试路径。
            token = load_token()
            resp = api_post("/ilink/bot/getupdates",
                            {"get_updates_buf": buf, "base_info": base_info(), "token": token},
                            token, timeout=40)
            if resp.get("ret") not in (0, None):
                print(f"[warn] getupdates ret={resp.get('ret')}", flush=True)
                time.sleep(5)
                continue
            batch_failed = False
            for msg in resp.get("msgs", []):
                try:
                    on_wechat_message(msg)
                except Exception as e:
                    batch_failed = True
                    print(f"[error] msg handler: {e}", flush=True)
            if batch_failed:
                print("[warn] batch incomplete; keeping updates cursor for replay", flush=True)
                time.sleep(5)
                continue
            # 全批成功才推进；先持久化，再更新内存，保存失败时也从原游标重试。
            next_buf = resp.get("get_updates_buf", buf)
            save_updates_buf(next_buf)
            buf = next_buf
        except Exception as e:
            print(f"[error] poll: {e}", flush=True)
            time.sleep(5)
