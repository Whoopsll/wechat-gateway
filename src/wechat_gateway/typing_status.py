"""输入状态与后台保活。"""

import threading
import time
from .api import api_post, base_info, load_token
from .config import TYPING_MAX_SECS, TYPING_TICK
from .storage import db

# ---------- 输入状态 ----------
# 注意：getconfig / sendtyping 的请求字段名按 OpenClaw 微信插件源码核对一遍
def get_typing_ticket(user_id, ctx):
    resp = api_post("/ilink/bot/getconfig",
                    {"ilink_user_id": user_id, "context_token": ctx, "base_info": base_info()},
                    load_token(), timeout=10)
    return resp.get("typing_ticket", "")

def send_typing(user_id, ticket, status):
    """status: 1 开始 / 2 取消"""
    api_post("/ilink/bot/sendtyping",
             {"ilink_user_id": user_id, "typing_ticket": ticket, "status": status,
              "base_info": base_info()},
             load_token(), timeout=10)

class TypingManager:
    """常驻在 poll 进程里：有未回复消息的聊天就持续显示"正在输入"，回复后或超时自动取消"""
    def __init__(self):
        self.active = {}   # chat_id -> {"ticket":..., "started":...}
        self.lock = threading.Lock()

    def start(self, chat_id, ctx):
        with self.lock:
            if chat_id in self.active:
                return
        try:
            ticket = get_typing_ticket(chat_id, ctx)
            if not ticket:
                return
            send_typing(chat_id, ticket, 1)
            with self.lock:
                self.active[chat_id] = {"ticket": ticket, "started": time.time()}
        except Exception as e:
            print(f"[typing] start failed: {e}", flush=True)

    def _waiting(self, chat_id):
        row = db().execute("SELECT 1 FROM inbox WHERE chat_id=? AND status IN ('pending','processing') "
                           "LIMIT 1", (chat_id,)).fetchone()
        return row is not None

    def tick(self):
        with self.lock:
            items = list(self.active.items())
        for chat_id, st in items:
            try:
                expired = time.time() - st["started"] > TYPING_MAX_SECS
                if expired or not self._waiting(chat_id):
                    send_typing(chat_id, st["ticket"], 2)
                    with self.lock:
                        self.active.pop(chat_id, None)
                else:
                    send_typing(chat_id, st["ticket"], 1)   # 保活
            except Exception as e:
                print(f"[typing] tick failed: {e}", flush=True)
                with self.lock:
                    self.active.pop(chat_id, None)

    def run(self):
        while True:
            time.sleep(TYPING_TICK)
            self.tick()

typing = TypingManager()
