# WeChat Gateway

从 `微信网关代码.html` 中提取的 Python 项目。HTML 保留为原始代码快照，后续修改以 `src/wechat_gateway/` 为准。

当前继续使用 Python、SQLite 和 curl。网关以 CLI 和长轮询工作，现阶段无需 Web 框架。以后增加 HTTP 接口或管理后台时，再单独加入 FastAPI。

## 项目结构

```text
wechat-gateway/
├── pyproject.toml                 # 安装配置、可选依赖和 wechat-gateway 命令
├── .env.example                  # 环境变量示例
├── wechat_gateway.py                 # 源码运行入口，无需安装包即可运行
├── src/wechat_gateway/
│   ├── cli.py                    # 命令分发
│   ├── config.py                 # 配置和默认参数
│   ├── storage.py                # SQLite、同步游标和 context_token
│   ├── api.py                    # curl 传输和微信 API
│   ├── poll.py                   # 单实例锁、长轮询
│   ├── inbox.py                  # 消息去重和入库
│   ├── claims.py                 # 认领、心跳、回收、wait_claim
│   ├── messages.py               # say / reply 和发送防重
│   ├── typing_status.py          # “正在输入”保活
│   ├── media.py                  # 收媒体、解密和内容解析
│   ├── files.py                  # 压图、上传和发文件
│   ├── mail.py                   # 原环境邮件 CLI 适配
│   ├── runner.py                 # 长命令和自动心跳
│   ├── reminders.py              # 提醒创建、notify 和派发
│   ├── login.py                  # 扫码登录和凭据持久化
│   └── qrcode.py                 # 二维码编码和 PNG 输出
├── hooks/wechat-inbound.sh       # 原 Hatch worker 唤醒脚本
├── tests/                        # 拆分后的离线验证
└── 微信网关代码.html              # 原始快照
```

## 安装

运行环境：Linux、Python 3.11+、curl。现有实现使用 `fcntl.flock` 和进程组，不直接支持 Windows。

```bash
cd wechat-gateway
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[media]'
cp .env.example .env
```

基础文本和登录功能只用标准库；`media` 扩展安装 AES 加解密所需的 pycryptodome，以及图片压缩所需的 Pillow。原有的 cryptography 回退路径也保留。

编辑 `.env`，设置账号、运行目录；如需邮件，设置允许的收件人和邮件 CLI。随后在同一终端载入配置：

```bash
set -a
source .env
set +a
wechat-gateway --help
```

`.env` 不自动加载。服务、定时器和 hook 都需要继承同一套环境变量。默认目录沿用旧部署路径，以便继续使用既有数据库和凭据；新部署可将两个目录改成项目内 `data/watch` 和 `data/accounts` 的绝对路径。CLI 会创建运行目录。

尚未安装包时，也可以从源码直接运行：

```bash
python3 wechat_gateway.py --help
```

安装后也支持 `python -m wechat_gateway`；三种入口使用相同的命令参数。

## 登录与收消息

```bash
wechat-gateway login
```

使用手机微信扫描 `$WECHAT_WATCH_DIR/login-qr.png`，如提示配对码则在终端输入。成功后会打印归一化的账号 ID，凭据写入 `$WECHAT_STATE_DIR/<账号ID>.json`。

把返回的 ID 写进 `.env` 的 `WECHAT_ACCOUNT_ID`，重新载入环境，再启动收消息：

```bash
set -a
source .env
set +a
wechat-gateway poll
```

凭据过期重登后，需要重启 `poll`；当前主循环会缓存启动时的 token。账号切换时也需要重新启动进程，让同步文件路径和账号一致。

`poll` 负责入库，worker 负责认领和回复。项目不包含独立的 AI worker 实现；原部署通过 Hatch hook 唤醒 worker。

## Worker 对接

原脚本的命令参数保留，包括：

```bash
wechat-gateway claim
wechat-gateway busy <claim_id>
wechat-gateway hold <claim_id> 300
wechat-gateway run <claim_id> -- python3 task.py
wechat-gateway say <claim_id> '正在处理'
wechat-gateway reply <reply_to> <chat_id> '处理完成'
wechat-gateway wait_claim --session <首批claim_id> --seconds 40
wechat-gateway send_file <claim_id> /absolute/path/to/file
```

`hooks/wechat-inbound.sh` 需要 Hatch 提供 `HATCH_HOOK_RUNTIME`、`wake` 和 `silent`。设置 `WECHAT_GATEWAY_SCRIPT` 为本项目 `wechat_gateway.py` 的绝对路径、`WECHAT_PYTHON` 为虚拟环境解释器路径；worker 也要继承相同配置。这个 hook 不能在普通 shell 中独立唤醒 AI。

## 定时提醒

有既往聊天记录后，可创建提醒：

```bash
wechat-gateway remind --chat-id <chat_id> --delay-seconds 600 --text '喝水啦'
wechat-gateway remind --chat-id <chat_id> --at '2026-11-09 15:00' --text '开会啦'
wechat-gateway dispatch_reminders
```

`--at` 按北京时间解析。提醒写入 `$WECHAT_WATCH_DIR/reminders/`。必须由外部 cron 或 systemd timer 每分钟执行一次 `dispatch_reminders`；`poll` 本身不会派发提醒。本次拆分不安装任何系统定时任务。

## 验证

从项目根目录运行，不需要安装测试框架：

```bash
python3 -m unittest discover -s tests -v
bash -n hooks/wechat-inbound.sh
```

测试使用临时目录、临时 SQLite 和模拟的网络、邮件接口，不向真实微信或邮箱发送消息。其中迁移校验逐一对比 HTML 中原有函数与类的语法树，确认提取没有遗漏或改变业务函数。

## 本次迁移范围与已知问题

本次只进行源码提取、模块拆分、入口和配置整理。保留原有业务函数及命令参数，新增环境变量配置、帮助入口和运行目录初始化。HTML 中提到的“148 项测试”未随文件提供，本项目没有将其计为已通过测试。

此前审查发现的问题仍需后续修复：

- `reply` 未校验原认领身份，过期 worker 可能结束新 worker 的批次。
- 消息处理失败后仍推进同步游标，可能丢消息。
- 部分 curl 异常被当成明确发送失败，可能导致重发。
- `poll` 缓存旧 token，重登后需重启；普通 API 也尚未采用凭据中的 `baseUrl`。
- HTTP 响应解析会搜索正文中的 `HTTP/`，可能破坏二进制内容。

下一步可在各自模块逐项修复，并为修复增加针对性的回归测试。
