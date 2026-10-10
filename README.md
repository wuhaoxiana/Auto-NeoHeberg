# NeoHeberg 多账号自动续期 + 开机守护

面板 `dash.neoheberg.fr` 的多个 LXC 容器，按计划自动续期与保活（GitHub Actions）。

## 功能

每次运行遍历 `NEO_ACCOUNTS` 里的**所有账号**，逐个执行：

1. **固定 IP 代理** — 经节点出口登录（可选；未配置则直连，全流程共用）
2. **登录** — 无头 Chrome 打开登录页，自动解 Cap.js 验证码并提交账号密码
3. **续期** — 剩余天数 ≤ 该账号的续期阈值（`renew_days`，默认取 `RENEW_DAYS`；
   workflow 里设为 14）时续期，+31 天且不损失已有天数
4. **开机守护** — 容器已停止则下发开机指令，45 秒后复验状态
5. **Telegram 通知** — 全部账号跑完后汇总成**一条**消息：每个账号的动作、
   到期日、容器状态、代理状态

单个账号失败不影响其余账号：登录失败/续期失败只记录在该账号条目里，
循环继续；只要有一个账号硬失败，退出码为 1，workflow 标红。

## 多账号配置

账号写在 **`NEO_ACCOUNTS`** 这个 Repository Secret 里，是一个 JSON 数组：

```json
[
  { "name": "主号",   "user": "a@example.com", "password": "pw1", "vmid": "12345" },
  { "name": "备用",   "user": "b@example.com", "password": "pw2", "vmid": "67890",
    "renew_days": 7 },
  { "name": "小号",   "user": "c@example.com", "password": "pw3", "vmid": "11111",
    "enabled": false }
]
```

每项字段：

| 字段 | 必填 | 说明 | 别名 |
|---|---|---|---|
| `name` | 否 | 账号标签，仅用于日志与通知（**别填敏感信息**） | `label` / `remark` |
| `user` | 是 | 面板登录账号 | `username` / `identifier` / `email` / `account` |
| `password` | 是 | 面板登录密码 | `pass` / `pwd` / `secret` |
| `vmid` | 是 | 容器实例 ID | `id` / `vm` / `vps_id` / `instance` |
| `type` | 否 | 服务类型，默认 `NEO_TYPE`（`vps`） | — |
| `renew_days` | 否 | 该账号续期阈值（天），默认 `RENEW_DAYS` | `days` / `threshold` |
| `enabled` | 否 | `false` 则跳过该账号（默认 `true`） | — |

也支持对象写法（key 作为 `name`）：

```json
{ "主号": { "user": "a@example.com", "password": "pw1", "vmid": "12345" } }
```

### 本地测试

不便用 Secret 时，复制 `accounts.example.json` 成 `accounts.json`（已在
`.gitignore` 里，不会被提交），用 `NEO_ACCOUNTS_FILE` 指向它：

```bash
cp accounts.example.json accounts.json   # 然后填入真实账号
export NEO_ACCOUNTS_FILE=accounts.json
export RENEW_DAYS=10
python3 renew.py --list      # 只打印解析出的账号（脱敏）
python3 renew.py             # 跑全部账号
python3 renew.py --only 主号  # 只跑某个 name 的账号
```

### 兼容旧配置

只配了 `NEO_USER` / `NEO_PASSWORD` / `NEO_VMID`（没有 `NEO_ACCOUNTS`）时，
脚本会把它当成一个名为 `default` 的账号处理 —— 老的 secret 不用改也能继续跑。

## 为什么用无头浏览器登录

面板登录带 **Cap.js 工作量证明验证码**。`POST /redeem` 会校验 `instr` 指纹字段——
服务端下发一段 base64 + deflate 的约 9KB 检测脚本，在沙箱 iframe 里采集
`navigator.webdriver` / 字体宽度 / `WebGL` 原生性 / `screen` 等信号，并检测
Node 环境泄漏。纯 HTTP 客户端无法作答（403 `missing_instrumentation_response`）。

无头 Chromium 能通过检测（检测针对的是 Node 运行时，不是无头浏览器引擎），
实测约 11 秒解出 `cap-token`。只有登录这一步需要浏览器，其余面板操作走纯 HTTP。

**不保存任何 cookie**：每次运行、每个账号都重新登录，因此不依赖任何会过期的
持久化凭证。

> 账号之间默认间隔 `NEO_ACCOUNT_DELAY`（20 秒）再开始下一个，避免同一出口 IP
> 短时间连续登录多个账号被限流/风控。按账号数估算 Actions 耗时：
> 每账号约 1~2 分钟（登录 11s + 解验证码 + 续期/开机复验），10 个账号约 15 分钟。

## 固定出口 IP（防风控）

面板会对「同一账号反复从不同机房 IP 登录」做风控标记。让每次运行的来源 IP
保持一致（固定节点出口），可以显著降低触发概率。**所有账号共用同一个出口。**

实现要点：

- 节点由 workflow 里的 `setup_proxy.sh` 拉起的 **sing-box** 提供，监听
  `127.0.0.1:1080`，导出 `PROXY_SERVER` / `IS_PROXY` 两个变量。
- `socks_proxy.py` 负责读取这两个变量并做两件事：
  - **HTTP 层** — 用 PySocks 的 `SocksiPyHandler` 构建 opener，`renew.py` 所有
    `urllib` 请求都走它；
  - **浏览器层** — 返回 `{"server": "socks5://…"}` 交给 Playwright 的 `proxy`
    参数，使无头 Chrome 流量也走固定出口。
- **不做全局 socket monkeypatch**：那会把 Playwright 到本地 driver 的连接一并
  劫持，导致浏览器启动失败。两条路径分开显式处理。
- 未配置 `NODE_LINK` 时静默退回直连 —— 功能可以先上线，secret 后补。

### 节点配置

`NODE_LINK` 是标准分享链接，`setup_proxy.sh` 会自动识别协议：

```
vless://<uuid>@<host>:<port>?type=tcp&security=reality&sni=...&pbk=...&fp=chrome#name
vmess://<base64 json>
trojan://<pw>@<host>:<port>?sni=...&type=ws&path=...
ss://<base64>@<host>:<port>
socks5://<host>:<port>
```

换节点只需改 `NODE_LINK` 这一个 secret，脚本与 workflow 都不用动。

> **注意**：`NODE_LINK` 含完整节点凭证（UUID、Reality 公钥、端口）。
> 本仓库是公开的，因此它必须放在 **Repository Secret**，绝不能写进代码或
> 提交到仓库。日志里只输出脱敏摘要（`vless @ 2a0***:22641`）。

## 配置

### Secrets

| Secret | 说明 |
|---|---|
| `NEO_ACCOUNTS` | **多账号 JSON 数组**（见上，推荐） |
| `NEO_USER` | 旧版单账号：面板登录账号（兼容保留） |
| `NEO_PASSWORD` | 旧版单账号：密码（兼容保留） |
| `NEO_VMID` | 旧版单账号：容器实例 ID（兼容保留） |
| `NODE_LINK` | 节点分享链接（可选；配了就走固定 IP，不配直连） |
| `TG_BOT_TOKEN` | Telegram bot token（可选，不配则跳过通知） |
| `TG_CHAT_ID` | Telegram chat id（可选） |

`NEO_ACCOUNTS` 与旧版三件套同时存在时，**以 `NEO_ACCOUNTS` 为准**。

### 可调环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `RENEW_DAYS` | 10（workflow 设 14） | 默认续期阈值（天） |
| `NEO_ACCOUNT_DELAY` | 20 | 账号之间间隔秒数 |
| `NEO_LOGIN_ATTEMPTS` | 4 | 单账号登录重试次数 |
| `NEO_LOGIN_BACKOFF` | 15,45,120,300 | 各次重试前的等待秒数 |
| `NEO_TYPE` | vps | 默认服务类型 |

### 命令行

```bash
python3 renew.py             # 跑全部账号
python3 renew.py --list      # 打印解析出的账号（脱敏）后退出
python3 renew.py --only 主号  # 只跑指定 name 的账号
python3 renew.py --selftest  # 只测验证码求解（不登录、不改任何状态）
```

workflow 也支持手动触发：`mode=selftest` 走自检，`only=<name>` 只跑某个账号。

单独检查代理是否可用：

```bash
PROXY_SERVER=socks5://127.0.0.1:1080 python3 socks_proxy.py
```

### 工作流末尾的两个收尾步骤

`renew.yml` 在续期任务之后还有两步（都带 `if: always()`，前面的步骤失败也会执行）：

- **更新时间** — 把北京时间写入 `time.txt` 并提交推送。GitHub 对「60 天无提交」
  的仓库会暂停定时工作流，这一步让它保持活跃。
- **清理旧运行记录** — 用 `gh run delete` 删掉除最新一条之外的历史运行，
  避免 Actions 记录无限堆积。

这两步需要写权限，因此 workflow 顶层显式声明了
`permissions: contents: write` + `actions: write`（`GITHUB_TOKEN` 默认只读）。

> 注意：`git push` 用 `GITHUB_TOKEN` 提交，不会再次触发 workflow（GitHub 会
> 忽略由该 token 产生的 push 事件），所以不会形成循环。

## 接口备忘

```
GET  /                                   面板 CSRF: var CSRF = "..."（或表单里的 csrf_token）
GET  /app/services/vps-stats.php?id=<id>  {"status":"running|stopped|starting|stopping|restarting",...}
POST /app/services/vps-power.php         vmid=<id>&signal=start|shutdown|reboot&csrf_token=...
POST /services/renew                     csrf_token=...&type=vps&id=<id>   → +31 天
```

登录为两步式 PHP 表单：先填 `identifier` 并点 `#goToPassword`，密码字段才会显示；
提交时带 `csrf_token` / `identifier` / `password` / `remember_me` / `cap-token`。
成功后会拿到 `__Host-NH` / `__Host-NH-Remember` cookie（后者 30 天）。

## 注意

- **停机 ≠ 网络故障**：容器关机后 IPv6 仍可能回 ping 但所有 TCP 端口不通，
  同网段邻居也不可达 —— 看起来像路由故障。判断容器状态要查面板
  `vps-stats.php` 的 `status` 字段，不要靠网络探测猜。
- 容器若装了自己的隧道守护（systemd timer），首次开机后隧道服务可能因网络未就绪
  而退出，需等其自愈。
- 续期为免费（面板显示 `offre gratuite`），余额 0 不影响。
- 日志会遮盖账号与实例 ID（`mask()`），因为本仓库公开、Actions 日志对外可见。
  节点信息只打印协议与脱敏地址，**不含 UUID / 密钥**；账号的 `name` 标签会原样
  打印，因此不要在里面写敏感信息。
