"""长命令运行、超时和任务心跳。"""

import os
import signal
import subprocess
import sys
import time
from .claims import heartbeat
from .config import RUN_BEAT_SECS, RUN_MAX_SECS

# ---------- 长命令包装 ----------
def run_with_heartbeat(claim_id, cmd):
    """运行一个长命令，期间每 RUN_BEAT_SECS 秒代为心跳。
    返回命令自己的退出码；被别人接手/已完成时杀掉命令返回 125；超过 RUN_MAX_SECS 强制结束返回 124"""
    first = heartbeat(claim_id)
    if first != "ok":
        print(f"[run] not started: {first}", file=sys.stderr)
        return 125
    proc = subprocess.Popen(cmd, start_new_session=True)   # 独立进程组，杀的时候连子进程一起杀
    start = last_beat = time.time()

    def stop(reason, code):
        print(f"[run] stopping command: {reason}", file=sys.stderr)
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        except ProcessLookupError:
            pass
        return code

    while True:
        try:
            rc = proc.wait(timeout=0.5)
            heartbeat(claim_id)          # 结束时再续一次，给 worker 留出收尾时间
            return rc
        except subprocess.TimeoutExpired:
            pass
        now = time.time()
        if now - start > RUN_MAX_SECS:
            return stop(f"exceeded {RUN_MAX_SECS}s", 124)
        if now - last_beat >= RUN_BEAT_SECS:
            last_beat = now
            hb = heartbeat(claim_id)
            if hb != "ok":
                return stop(f"heartbeat={hb}, someone else owns this batch", 125)
