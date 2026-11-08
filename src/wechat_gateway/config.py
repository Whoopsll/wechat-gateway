"""环境配置和默认运行参数。"""

import os

BASE_URL = os.environ.get("WECHAT_BASE_URL", "https://ilinkai.weixin.qq.com").rstrip("/")
ACCOUNT_ID = os.environ.get("WECHAT_ACCOUNT_ID", "<bot-account-id>")
STATE_DIR = os.path.expanduser(os.environ.get("WECHAT_STATE_DIR", "~/.openclaw/openclaw-weixin/accounts"))
WATCH_DIR = os.path.expanduser(os.environ.get("WECHAT_WATCH_DIR", "~/workspace/wechat-watch"))
DB_PATH = os.path.join(WATCH_DIR, "wechat.db")
SYNC_FILE = os.path.join(STATE_DIR, f"{ACCOUNT_ID}.sync.json")
CTX_TOKEN_FILE = os.path.join(STATE_DIR, f"{ACCOUNT_ID}.context-tokens.json")

ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (4 << 8) | 10

STALE_SECS = 120        # 超过这么久没有心跳（claim 或 busy）视为 worker 挂了，放回 pending
TYPING_TICK = 4         # 输入状态保活间隔（微信约 5 秒过期）
TYPING_MAX_SECS = 600   # 最长显示输入状态的时间（长任务有心跳时也会一直显示到这个上限）
HISTORY_N = 10          # 唤醒时附带的最近上下文条数

# 邮件：只允许发给有长期授权的地址，其他地址会弹确认卡、网关会卡住等确认
GWS_CLI = os.environ.get("WECHAT_MAIL_CLI", "/opt/hatch/bin/hatch_gws_cli")
MAIL_ALLOWED = {address.strip().lower() for address in os.environ.get("WECHAT_MAIL_ALLOWED", "you@example.com").split(",") if address.strip()}
MAIL_TIMEOUT = 120      # 秒；超时视为"不确定是否已发"，不自动重试
MAIL_MAX_BYTES = 25 * 1024 * 1024

# run 包装：长命令期间由网关代为心跳
RUN_BEAT_SECS = 50      # 心跳间隔，必须小于 STALE_SECS
RUN_MAX_SECS = 10 * 60  # 单个命令最长运行时间，到点强制结束（防止孤儿进程把聊天永久锁死）

# 媒体：收到的图片/文件解密后存这里；发文件走微信 CDN
MEDIA_DIR = os.path.join(WATCH_DIR, "media")
CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
MEDIA_DOWNLOAD_MAX = 50 * 1024 * 1024   # 收：单个媒体最大 50MB
SEND_FILE_MAX = 20 * 1024 * 1024        # 发：超过 20MB 走邮件
MEDIA_RETRY_SLEEP = 1                   # 截断后重下前等多久（秒）
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
