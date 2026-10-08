# NeoHeberg 自动续期 + 开机守护

面板 `dash.neoheberg.fr` 的 LXC VPS（`sbsb` / VMID 1355）每日自动续期与保活。

## 功能

每天 UTC 02:00（北京时间 10:00）运行一次：

1. **会话检查** — 用 cookie 访问面板
2. **cookie 自动刷新** — 失效则用无头 Chrome 自动登录（含 Cap.js PoW 验证码）并轮换 secret
3. **续期** — 剩余天数 ≤ `RENEW_DAYS`（默认 14）时续期，+31 天且不损失已有天数
4. **开机守护** — 容器已停止则下发开机指令
5. **Telegram 通知** — 汇报本次动作、到期日、容器状态

## 为什么需要无头浏览器

面板登录带 **Cap.js 工作量证明验证码**。`POST /redeem` 会校验 `instr` 指纹字段——
服务端下发一段 base64 + deflate 的 9KB 检测脚本，在沙箱 iframe 里采集
`navigator.webdriver` / `fonts` / `WebGL` / `screen` 等信号并检测 Node/headless 泄漏。
纯 HTTP 客户端无法作答（403 `missing_instrumentation_response`）。

解决办法：**只让浏览器做"解验证码"这一步**，拿到 `cap-token` 后所有面板操作仍走纯 HTTP。

## Secrets

| Secret | 说明 |
|---|---|
| `NEO_COOKIE` | 面板 Cookie 头（`__Host-NH=...; __Host-NH-Remember=...`），有效期 30 天，失效后自动轮换 |
| `NEO_PASSWORD` | 面板密码，仅用于 cookie 失效时重新登录 |
| `GH_PAT` | 有 `repo` scope 的 PAT，用于自动更新 `NEO_COOKIE` secret（可选，不配则只在日志提示） |
| `TG_BOT_TOKEN` | Telegram bot token（可选，不配则跳过通知） |
| `TG_CHAT_ID` | Telegram chat id（可选） |

`NEO_COOKIE` 首次手工获取：浏览器登录面板 → DevTools → Application → Cookies →
把 `__Host-NH` 和 `__Host-NH-Remember` 拼成 `name=value; name=value` 形式。

## 本地测试

```bash
export NEO_COOKIE="__Host-NH=...; __Host-NH-Remember=..."
export NEO_PASSWORD="..."
export RENEW_DAYS=10
python3 renew.py
```

无头浏览器测试（验证 cookie 自动刷新）：

```bash
export NEO_COOKIE="__Host-NH=invalid; __Host-NH-Remember=invalid"
python3 renew.py     # 应触发自动登录
```

## 接口备忘

```
GET  /                                   面板 CSRF: var CSRF = "..."（也可以是表单里的 csrf_token）
GET  /app/services/vps-stats.php?id=1355 状态: {"status":"running|stopped|starting|stopping|restarting",...}
POST /app/services/vps-power.php         vmid=1355&signal=start|shutdown|reboot&csrf_token=...
POST /services/renew                     csrf_token=...&type=vps&id=1355  → +31 天
```

## 注意

- **停机 ≠ 网络故障**：容器关机后 IPv6 仍可能回 ping 但所有 TCP 端口不通，
  同网段邻居也不可达 —— 看起来像路由故障。判断状态要查面板 `vps-stats.php`。
- 容器每次开机后 `argo.service` 会因网络未就绪而退出，靠容器内的
  `argo-watchdog.timer`（每 2 分钟）拉起隧道。
- 续期是免费的（`offre gratuite`），余额 0 coins 不影响。
