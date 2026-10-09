# NeoHeberg 多账号自动续期 + 开机守护

面板 `dash.neoheberg.fr` 的 LXC 容器**多账号**每日自动续期与保活（GitHub Actions）。

## 功能

每天 UTC 02:00（北京时间 10:00）运行一次，对配置里的每个账号依次执行：

1. **登录** — 无头 Chrome 打开登录页，自动解 Cap.js 验证码并提交账号密码
2. **续期** — 剩余天数 ≤ `RENEW_DAYS`（默认 14）时续期，+31 天且不损失已有天数
3. **开机守护** — 容器已停止则下发开机指令，45 秒后复验状态
4. **Telegram 通知** — **每个账号单独一条**通知，汇报动作、到期日、容器状态

同一账号的多个实例只登录一次（同一浏览器 context 内复用会话），
不同账号之间 cookie 完全隔离，不会串会话。

## 为什么用无头浏览器登录

面板登录带 **Cap.js 工作量证明验证码**。`POST /redeem` 会校验 `instr` 指纹字段——
服务端下发一段 base64 + deflate 的约 9KB 检测脚本，在沙箱 iframe 里采集
`navigator.webdriver` / 字体宽度 / `WebGL` 原生性 / `screen` 等信号，并检测
Node 环境泄漏。纯 HTTP 客户端无法作答（403 `missing_instrumentation_response`）。

无头 Chromium 能通过检测（检测针对的是 Node 运行时，不是无头浏览器引擎），
实测约 11 秒解出 `cap-token`。只有登录这一步需要浏览器，其余面板操作走纯 HTTP。

**不保存任何 cookie**：每次运行都重新登录，因此不依赖任何会过期的持久化凭证。

## 配置

### Secrets

多账号配置**按行一一对应**：第 1 行账号 → 第 1 行密码 → 第 1 行实例 ID。

| Secret | 说明 |
|---|---|
| `NEO_USER` | 面板登录账号，每行一个 |
| `NEO_PASSWORD` | 面板登录密码，每行一个 |
| `NEO_VMID` | 容器实例 ID，每行一个 |
| `NEO_TYPE` | 实例类型，每行一个（可选，默认 `vps`） |
| `NEO_LABEL` | 显示名，每行一个（可选，默认用 VMID） |
| `TG_BOT_TOKEN` | Telegram bot token（可选，不配则跳过通知） |
| `TG_CHAT_ID` | Telegram chat id（可选） |

行数必须匹配：`NEO_USER` / `NEO_PASSWORD` / `NEO_VMID` 三者行数不一致会直接报错退出，
避免跑错账号。`NEO_TYPE` / `NEO_LABEL` 行数不足时按 `vps` / VMID 补齐。

也支持用逗号 / 分号分隔写在一行（`a@x.com,b@y.com`），脚本会自动拆行。

### 手动触发

Actions 页面手动运行时可填 `accounts` 输入框选择账号序号：

- 留空 = 全部账号
- `1,3` = 第 1 和第 3 个账号
- `2-4` = 第 2 到第 4 个账号

序号非法或超出范围会直接报错退出，不会误跑全部账号。

### 本地测试

```bash
export NEO_USER="a@x.com"          # 多账号用换行分隔
export NEO_PASSWORD="pass1"
export NEO_VMID="12345"
export RENEW_DAYS=14
python3 renew.py
```

只测验证码求解（不登录、不改任何状态）：

```bash
python3 renew.py --selftest
```

工作流也支持手动触发 `selftest` 模式。

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
- 每个账号登录 + 续期约 1 分钟，账号多时注意 workflow 的 `timeout-minutes`（当前 45）。
- 日志会遮盖账号与实例 ID（`mask_user()` / `mask()`），因为本仓库公开、Actions 日志对外可见。
- 全部账号都失败时脚本以非零码退出，Actions 会标红；只要有一个成功就算通过。
