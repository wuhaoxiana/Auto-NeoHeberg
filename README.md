# NeoHeberg 自动续期 + 开机守护

面板 `dash.neoheberg.fr` 的 LXC 容器每日自动续期与保活（GitHub Actions）。

## 功能

每天 UTC 02:00（北京时间 10:00）运行一次：

1. **会话检查** — 用 cookie 访问面板
2. **cookie 自动刷新** — 失效则用无头 Chrome 自动登录（含 Cap.js PoW 验证码）并轮换 secret
3. **续期** — 剩余天数 ≤ `RENEW_DAYS`（默认 14）时续期，+31 天且不损失已有天数
4. **开机守护** — 容器已停止则下发开机指令
5. **Telegram 通知** — 汇报本次动作、到期日、容器状态

## 为什么需要无头浏览器

面板登录带 **Cap.js 工作量证明验证码**。`POST /redeem` 会校验 `instr` 指纹字段——
服务端下发一段 base64 + deflate 的约 9KB 检测脚本，在沙箱 iframe 里采集
`navigator.webdriver` / 字体宽度 / `WebGL` 原生性 / `screen` 等信号，并检测
Node / headless 环境泄漏。纯 HTTP 客户端无法作答（403 `missing_instrumentation_response`）。

解决办法：**只让浏览器做"解验证码"这一步**，拿到 `cap-token` 后所有面板操作仍走纯 HTTP。

## 配置

### Secrets

| Secret | 说明 |
|---|---|
| `NEO_USER` | 面板登录账号 |
| `NEO_VMID` | 容器实例 ID（面板服务列表里可见） |
| `NEO_COOKIE` | 面板 Cookie 头（`__Host-NH=...; __Host-NH-Remember=...`），有效期 30 天，失效后自动轮换 |
| `NEO_PASSWORD` | 面板密码，仅用于 cookie 失效时重新登录 |
| `GH_PAT` | 有 `repo` scope 的 PAT，用于自动更新 `NEO_COOKIE`（可选，不配则只在日志提示） |
| `TG_BOT_TOKEN` | Telegram bot token（可选，不配则跳过通知） |
| `TG_CHAT_ID` | Telegram chat id（可选） |

`NEO_COOKIE` 首次获取：浏览器登录面板 → DevTools → Application → Cookies →
把 `__Host-NH` 与 `__Host-NH-Remember` 拼成 `name=value; name=value` 形式。

### 本地测试

```bash
export NEO_USER=...  NEO_VMID=...
export NEO_COOKIE="__Host-NH=...; __Host-NH-Remember=..."
export NEO_PASSWORD=...
export RENEW_DAYS=10
python3 renew.py
```

验证 cookie 自动刷新（应触发无头登录）：

```bash
export NEO_COOKIE="__Host-NH=invalid; __Host-NH-Remember=invalid"
python3 renew.py
```

## 接口备忘

```
GET  /                                   面板 CSRF: var CSRF = "..."（或表单里的 csrf_token）
GET  /app/services/vps-stats.php?id=<id>  {"status":"running|stopped|starting|stopping|restarting",...}
POST /app/services/vps-power.php         vmid=<id>&signal=start|shutdown|reboot&csrf_token=...
POST /services/renew                     csrf_token=...&type=vps&id=<id>   → +31 天
```

登录为两步式 PHP 表单：先填 `identifier` 并点 `#goToPassword`，密码字段才会显示；
提交时带 `csrf_token` / `identifier` / `password` / `remember_me` / `cap-token`。

## 注意

- **停机 ≠ 网络故障**：容器关机后 IPv6 仍可能回 ping 但所有 TCP 端口不通，
  同网段邻居也不可达 —— 看起来像路由故障。判断容器状态要查面板
  `vps-stats.php` 的 `status` 字段，不要靠网络探测猜。
- 容器若装了自己的隧道守护（systemd timer），首次开机后隧道服务可能因网络未就绪
  而退出，需等其自愈。
- 续期为免费（面板显示 `offre gratuite`），余额 0 不影响。
