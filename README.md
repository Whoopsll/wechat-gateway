# WeChat Gateway

WeChat Gateway 是一个连接微信与 AI 助手、自动化任务的轻量 Python 网关。它通过微信 ilink API 接收消息，将消息保存到本地 SQLite，由外部 worker 认领处理，再通过网关把回复、图片或文件发回微信。

项目负责消息接入、任务交接和结果送达，回复内容与具体任务由 worker 完成。仓库提供 Hatch 的 worker 唤醒脚本，也可以通过 CLI 对接其他能够执行命令的任务程序。

## 适用场景

- **微信 AI 助手**：在微信中发起对话，让接入的 AI worker 结合最近聊天记录回复。
- **远程任务入口**：通过微信提交文件处理、脚本执行等请求，查看处理进度并接收结果；实际执行能力由 worker 提供。
- **个人提醒与通知**：创建定时提醒，或让已有自动化流程向有既往会话的聊天对象发送通知。

## 主要功能

- **扫码登录**：生成登录二维码，处理扫码确认和配对码验证，保存账号凭据；同一账号刷新 token 后，收消息进程在下一轮请求采用新凭据。
- **消息接收与上下文**：长轮询接收消息，使用 SQLite 按消息 ID 去重，保存聊天历史，并在任务认领时附带最近上下文。
- **批量认领与任务保护**：同一聊天串行处理，批量认领待处理消息；支持心跳续期、长任务预约和超时回收，通过 `claim_id` 拒绝失效 worker 的回复。
- **回复与进度反馈**：发送中间进度和最终回复，在处理期间显示“正在输入”；回复后可短暂守窗，等待并处理下一批消息。
- **图片与文件收发**：下载、解密收到的图片和文件，支持图片压缩、CDN 上传及微信发送；超出微信发送限制的文件可通过邮件适配器发送，需配置原环境邮件 CLI 和收件人白名单。
- **定时提醒**：按相对时间或北京时间创建提醒规格，由外部定时器调用派发命令，在到期后主动发送。
- **长命令运行**：通过 `run` 执行命令并自动维持任务心跳，支持执行超时和认领失效时终止进程。
- **异常恢复与发送防重**：处理失败时保留收消息游标以便重放；发送结果不确定时保留防重记录，减少网络异常导致的重复发送。

## 工作方式

主要处理流程为：**微信消息 → 长轮询接收 → SQLite 入库 → worker 认领并执行 → 网关回复微信**。

网关使用 Python、SQLite 和 curl，以命令行和长轮询运行，无需 Web 框架。自动问答需要外部 worker，定时提醒需要外部 cron 或 systemd timer。语音仅接收消息中已有的转文字内容，视频暂不支持；图片和文件保留一次补发策略，发送结果不确定时仍可能重复，详见后文说明。

项目最初从 `微信网关代码.html` 中提取并拆分。HTML 保留为原始代码快照，后续开发以 `src/wechat_gateway/` 为准。

## 项目结构

```text
wechat-gateway/
├── pyproject.toml                 # 安装配置、可选依赖和 wechat-gateway 命令
├── .env.example                  # 环境变量示例
├── wechat_gateway.py             # 源码运行入口，无需安装包即可运行
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

同一账号重登或刷新 token 后，`poll` 会在下一轮长轮询读取新凭据，不需要为 token 刷新单独重启；如果正在长轮询，则等当前请求结束后生效。账号切换或环境变量变化时仍需重新启动进程，让同步文件路径和账号一致。普通 API 仍采用配置的 `WECHAT_BASE_URL`，尚未动态采用凭据里的 `baseUrl`。

`poll` 负责入库，worker 负责认领和回复。项目不包含独立的 AI worker 实现；原部署通过 Hatch hook 唤醒 worker。

## Worker 对接

命令示例（`reply` 现在必须显式提供当前批次的 `claim_id`）：

```bash
wechat-gateway claim
wechat-gateway busy <claim_id>
wechat-gateway hold <claim_id> 300
wechat-gateway run <claim_id> -- python3 task.py
wechat-gateway say <claim_id> '正在处理'
wechat-gateway reply <reply_to> <chat_id> --claim-id <claim_id> '处理完成'
wechat-gateway wait_claim --session <首批claim_id> --seconds 40
wechat-gateway send_file <claim_id> /absolute/path/to/file
```

`hooks/wechat-inbound.sh` 需要 Hatch 提供 `HATCH_HOOK_RUNTIME`、`wake` 和 `silent`。设置 `WECHAT_GATEWAY_SCRIPT` 为本项目 `wechat_gateway.py` 的绝对路径、`WECHAT_PYTHON` 为虚拟环境解释器路径；worker 也要继承相同配置。这个 hook 不能在普通 shell 中独立唤醒 AI。

`reply` 的 `--claim-id` 使用当前 payload 里的值；旧代次返回 `superseded`，不会发送或收尾新批次。缺少该参数的旧命令会报错并拒绝发送。部署此版本时必须同步更新 hook 和其他 worker 调用方，不能只更新 Python 包；建议让当前 worker 先结束，再切换 hook 与网关版本。

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

测试使用临时目录、临时 SQLite 和模拟的网络、邮件接口，不向真实微信或邮箱发送消息。其中迁移校验检查 HTML 中原有定义是否齐全，并逐一对比尚未修改的函数与类的语法树；已修复的定义由 `test_api.py`、`test_delivery.py`、`test_poll.py` 专项覆盖。

## HTTP 响应解析修复

`_parse_curl_headers` 已改为从头顺序解析响应头，跳过常见的代理 `200 Connection established` 头和 1xx 临时响应（101 协议切换除外）。最终响应头结束后立即固定正文起点，不再在正文中搜索 `HTTP/`，也不转换正文的换行或二进制字节。状态行、头字段和 curl 状态尾标会进行校验。

这修复了普通微信文字包含 `HTTP/` 时，`getupdates` 解析失败、同一批消息反复获取的问题，也避免了正文含伪响应头时被截断。回归用例按故障描述构造，不是线上原始响应。实现沿用当前 curl 的 HTTP/1.1、不跟随重定向参数，未扩展为通用的多请求输出解析器。

更新部署副本后需要重启网关，再确认解析错误不再增长、积压消息实际入库、游标推进，并用新微信消息验证收发；本地离线测试不代表线上已恢复。

## GW-1～GW-4 修复与部署说明

本轮修复代码已写入仓库工作区，线上状态以 Muse 的独立验证报告和部署记录为准。没有修改既有数据库结构或凭据格式。

- **GW-1**：`reply` 的代次校验、心跳续期和防重占位在同一 SQLite 写事务中完成；收尾只更新显式提供的原 `claim_id`。网络调用期间不持有 SQLite 写锁。已经进入传输的请求无法撤回；如果此时手动解除认领或强行接管，收尾也不会误更新新代次。
- **GW-2**：一批消息中任一处理失败，保留原游标，等待后重放；本轮其余消息仍可入库，重放由消息 ID 去重。`inbox` 和用户历史同事务提交。游标文件原子替换，保存成功后才更新内存游标。永久畸形消息会持续阻止该批游标前进并记录警告，需要人工排查；本轮没有自动跳过或清除消息。
- **GW-3**：curl 错误携带是否可能已发出请求的信息。52/55/56、未知传输错误、超时、HTTP 408/5xx、响应解析失败等进入 `uncertain`，保留防重记录；明确的请求前失败、HTTP 拒绝和 API 非零 `ret` 可撤销占位后重试。`ret` 省略或为 null 的对象继续按原协议兼容成功处理。凭据读取失败发生在请求前，不会伪装成送达不确定。
- **GW-4**：poll 每轮现读同一账号的 token，临时凭据读取失败也进入正常重试路径。

`uncertain` 表示无法确认是否送达，不保证必达。文字回复、进度消息和提醒不自动补发；图片和文件保留原有的一次主动补发策略，仍可能收到两份，这次没有改变该产品取舍。CDN 上传重试也维持原行为。

原始 HTML 仍是快照，其中“148 项测试”未随文件提供，不计入当前验证结果。本轮的测试可用上文命令重跑；生产部署须经用户确认，Muse 维护的 `test-reports/` 不纳入 Git。
