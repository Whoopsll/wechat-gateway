"""兼容原脚本参数的命令入口。"""

import json
import os
import sys
from .claims import claim_batch, heartbeat, hold, list_events, unclaim, wait_claim
from .config import STATE_DIR, WATCH_DIR
from .files import send_file
from .login import LOGIN_TIMEOUT_MINUTES, qr_login
from .mail import send_mail
from .messages import say, send_reply
from .poll import poll_loop
from .reminders import _beijing_str, dispatch_reminders, notify, remind_create
from .runner import run_with_heartbeat

USAGE = ("用法: poll | claim | busy <claim_id> | hold <claim_id> <秒，最多600> | run <claim_id> -- <命令...>"
         " | send_file <claim_id> <路径>（20MB 以内，图片按图片发）"
         " | send_mail <claim_id> --to X --subject Y [-a 文件]... [--html]（正文从 stdin 读）"
         " | say <claim_id> [text] | reply <reply_to> <chat_id> [text]（不给 text 则从 stdin 读）"
         " | unclaim <chat_id>（手动工具） | events [n]（查看异常事件）"
         " | login [--timeout-minutes N]（扫码登录/凭据过期重登，二维码写到 login-qr.png）"
         " | wait_claim --session <段落首批claim_id> --seconds 40（段落守窗：阻塞等下一批消息，整段封顶30分钟）"
         " | notify <chat_id> [--key <key>] [text]（主动发一条文本，无需认领）"
         " | remind --chat-id <id> (--delay-seconds N | --at \"YYYY-MM-DD HH:MM\") --text <内容>（建定时提醒）"
         " | dispatch_reminders（派发到期提醒，主侧每分钟调用）")

def main(argv=None):
    a = ["wechat-gateway", *(sys.argv[1:] if argv is None else argv)]
    if len(a) == 1 or a[1:] in (["--help"], ["-h"]):
        print(USAGE)
        return 0
    os.makedirs(WATCH_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    if len(a) == 2 and a[1] == "poll":
        poll_loop()
    elif len(a) == 2 and a[1] == "claim":
        print(json.dumps(claim_batch(), ensure_ascii=False))
    elif len(a) in (4, 5) and a[1] == "reply":
        text = a[4] if len(a) == 5 else sys.stdin.read()
        print(send_reply(a[2], a[3], text))
    elif len(a) in (3, 4) and a[1] == "say":
        text = a[3] if len(a) == 4 else sys.stdin.read()
        print(say(a[2], text))
    elif len(a) >= 5 and a[1] == "run" and a[3] == "--":
        sys.exit(run_with_heartbeat(a[2], a[4:]))
    elif len(a) == 4 and a[1] == "send_file":
        print(send_file(a[2], a[3]))
    elif len(a) >= 3 and a[1] == "send_mail":
        import argparse
        ap = argparse.ArgumentParser(prog="wechat_gateway.py send_mail")
        ap.add_argument("claim_id")
        ap.add_argument("--to", required=True)
        ap.add_argument("--subject", required=True)
        ap.add_argument("-a", "--attach", action="append", default=[])
        ap.add_argument("--html", action="store_true")
        o = ap.parse_args(a[2:])
        print(send_mail(o.claim_id, o.to, o.subject, sys.stdin.read(), o.attach, o.html))
    elif len(a) in (2, 3) and a[1] == "events":
        print(list_events(int(a[2]) if len(a) == 3 else 20))
    elif len(a) == 4 and a[1] == "hold":
        print(hold(a[2], a[3]))
    elif len(a) == 3 and a[1] == "busy":
        print(heartbeat(a[2]))
    elif len(a) == 3 and a[1] == "unclaim":
        unclaim(a[2]); print("reset to pending")
    elif len(a) >= 2 and a[1] == "wait_claim":
        import argparse
        ap = argparse.ArgumentParser(prog="wechat_gateway.py wait_claim")
        ap.add_argument("--session", required=True, help="段落首批的 claim_id，同一轮 worker 全程不变")
        ap.add_argument("--seconds", type=int, default=40, help="本次守窗秒数（1-120，默认 40）")
        o = ap.parse_args(a[2:])
        print(json.dumps(wait_claim(o.session, o.seconds), ensure_ascii=False))
    elif len(a) >= 2 and a[1] == "notify":
        import argparse
        ap = argparse.ArgumentParser(prog="wechat_gateway.py notify")
        ap.add_argument("chat_id")
        ap.add_argument("--key", default=None)
        ap.add_argument("text", nargs="?")
        o = ap.parse_args(a[2:])
        text = o.text if o.text is not None else sys.stdin.read()
        try:
            print(notify(o.chat_id, text, o.key))
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
    elif len(a) >= 2 and a[1] == "remind":
        import argparse
        ap = argparse.ArgumentParser(prog="wechat_gateway.py remind")
        ap.add_argument("--chat-id", required=True)
        ap.add_argument("--delay-seconds", type=int, default=None)
        ap.add_argument("--at", default=None)
        ap.add_argument("--text", required=True)
        o = ap.parse_args(a[2:])
        try:
            spec = remind_create(o.chat_id, o.text, delay_seconds=o.delay_seconds, at=o.at)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps({"id": spec["id"], "due_epoch": spec["due_epoch"],
                          "due_local": _beijing_str(spec["due_epoch"])}, ensure_ascii=False))
    elif len(a) == 2 and a[1] == "dispatch_reminders":
        print(json.dumps(dispatch_reminders(), ensure_ascii=False))
    elif len(a) >= 2 and a[1] == "login":
        import argparse
        ap = argparse.ArgumentParser(prog="wechat_gateway.py login")
        ap.add_argument("--timeout-minutes", type=float, default=LOGIN_TIMEOUT_MINUTES)
        o = ap.parse_args(a[2:])
        r = qr_login(timeout_minutes=o.timeout_minutes)
        if r["result"] == "connected":
            print(f"connected: {r['account_id']}")
        elif r["result"] == "already_connected":
            print("already_connected")
        else:
            print(f"failed: {r['message']}")
            sys.exit(1)
    else:
        print(USAGE)
