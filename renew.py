#!/usr/bin/env python3
"""
NeoHeberg 多账号 VPS 自动续期 + 开机守护

每次运行（GitHub Actions 每日/每周一次）遍历所有账号，对每个账号：
  1. 无头浏览器登录面板（每次重新登录，不持久化 cookie）
  2. 检查到期日，剩余天数 <= 续期阈值 则续期（+31 天）
  3. 检查容器状态，已停止则开机
最后汇总成一条 Telegram 通知。

账号来源（优先级从高到低）：
  NEO_ACCOUNTS       账号数组的 JSON 字符串（GitHub Secret，推荐）
  NEO_ACCOUNTS_FILE  账号数组的 JSON 文件路径（本地测试用）
  NEO_USER / NEO_PASSWORD / NEO_VMID   旧版单账号配置（兼容保留）

账号 JSON 每项字段（别名见 README）：
  name       账号标签（仅用于日志/通知，勿填敏感信息）
  user       面板登录账号
  password   面板登录密码
  vmid       容器实例 ID
  type       服务类型，默认取 NEO_TYPE（vps）
  renew_days 该账号的续期阈值（天），默认取 RENEW_DAYS
  enabled    是否启用，默认 true

环境变量：
  NEO_ACCOUNTS       多账号 JSON（见上）
  NEO_ACCOUNTS_FILE  多账号 JSON 文件路径
  NEO_USER/NEO_PASSWORD/NEO_VMID  旧版单账号（兼容）
  NEO_TYPE           默认服务类型，默认 vps
  NODE_LINK          节点分享链接（可选，固定出口 IP 防风控）
  PROXY_SERVER       本地代理地址（由 setup_proxy.sh 导出，优先于 NODE_LINK 探测）
  TG_BOT_TOKEN       Telegram bot token（可留空则不通知）
  TG_CHAT_ID         Telegram chat id
  RENEW_DAYS         默认续期阈值，默认 10
  NEO_ACCOUNT_DELAY  账号之间间隔秒数，默认 20（降低风控/限流概率）

命令行：
  --selftest         只测无头浏览器 + Cap.js 求解（用第一个账号的用户名，不登录）
  --only <name>      只处理指定 name 的账号
  --list             打印解析出的账号（脱敏）后退出
"""

import os
import re
import sys
import json
import time
import urllib.request
import urllib.parse
import urllib.error
import socks_proxy  # 本地模块：NODE_LINK 代理（可选，未配置则直连）

BASE = "https://dash.neoheberg.fr"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 全局默认值（账号项未指定时回退到这里）
RENEW_DAYS = int(os.environ.get("RENEW_DAYS", "10"))
DEFAULT_TYPE = os.environ.get("NEO_TYPE", "vps")
ACCOUNT_DELAY = int(os.environ.get("NEO_ACCOUNT_DELAY", "20"))

TRIGGER = os.environ.get("GITHUB_EVENT_NAME", "local")
RUN_URL = ""
if os.environ.get("GITHUB_RUN_ID"):
    RUN_URL = (f"{os.environ.get('GITHUB_SERVER_URL','https://github.com')}/"
               f"{os.environ.get('GITHUB_REPOSITORY','')}/actions/runs/"
               f"{os.environ['GITHUB_RUN_ID']}")

LOG = []

# 当前正在处理的账号标签，用于日志前缀
CURRENT = {"tag": ""}


def mask(s, show=2):
    """日志脱敏：只留首尾各 show 位"""
    s = str(s)
    if not s:
        return "(空)"
    if len(s) <= show * 2:
        return "*" * len(s)
    return s[:show] + "*" * (len(s) - show * 2) + s[-show:]


def log(msg):
    tag = CURRENT["tag"]
    line = f"[{time.strftime('%H:%M:%S')}]{f'[{tag}]' if tag else ''} {msg}"
    print(line, flush=True)
    LOG.append(line)


# ───────────────────────── 账号配置 ─────────────────────────

def _first(d, *keys, default=""):
    """按顺序取第一个非空字段（支持多个别名）"""
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def _as_bool(v, default=True):
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(v)


def _as_int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def normalize_account(item, idx):
    """把一条原始配置规整成内部账号 dict（含字段别名）"""
    if not isinstance(item, dict):
        return None
    return {
        "index": idx,
        "name": str(_first(item, "name", "label", "remark", default=f"账号{idx}")),
        "user": str(_first(item, "user", "username", "identifier", "email", "account")).strip(),
        "password": str(_first(item, "password", "pass", "pwd", "secret")),
        "vmid": str(_first(item, "vmid", "id", "vm", "vps_id", "instance")).strip(),
        "type": str(_first(item, "type", default=DEFAULT_TYPE)).strip() or DEFAULT_TYPE,
        "renew_days": _as_int(_first(item, "renew_days", "days", "threshold",
                                     default=RENEW_DAYS), RENEW_DAYS),
        "enabled": _as_bool(item.get("enabled"), True),
    }


def load_accounts():
    """解析账号列表。返回 (accounts, source_desc)。"""
    raw = (os.environ.get("NEO_ACCOUNTS") or "").strip()
    src = "NEO_ACCOUNTS"

    if not raw:
        path = (os.environ.get("NEO_ACCOUNTS_FILE") or "").strip()
        if path:
            src = path
            try:
                with open(path, encoding="utf-8") as f:
                    raw = f.read()
            except OSError as e:
                raise SystemExit(f"无法读取 {path}: {e}")

    accounts = []
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise SystemExit(f"{src} 不是合法 JSON: {e}")
        # 允许 {"名字": {...}} 的对象写法
        if isinstance(data, dict):
            flat = []
            for k, v in data.items():
                if isinstance(v, dict):
                    flat.append(dict(v, name=_first(v, "name", "label", default=k)))
                else:
                    raise SystemExit(f"{src} 的对象写法要求值为对象：{k}")
            data = flat
        if not isinstance(data, list):
            raise SystemExit(f"{src} 需为 JSON 数组或对象")
        for i, item in enumerate(data, 1):
            a = normalize_account(item, i)
            if a:
                accounts.append(a)
            else:
                raise SystemExit(f"{src} 第 {i} 项不是对象")
        return accounts, f"{src}（{len(accounts)} 个账号）"

    # 兼容旧版单账号 secret
    legacy = {"user": os.environ.get("NEO_USER", ""),
              "password": os.environ.get("NEO_PASSWORD", ""),
              "vmid": os.environ.get("NEO_VMID", "")}
    if any(v.strip() for v in legacy.values()):
        a = normalize_account(dict(legacy, name="default"), 1)
        return [a], "旧版单账号环境变量"

    return [], "未配置"


def missing_fields(acct):
    return [k for k, v in (("user", acct["user"]), ("password", acct["password"]),
                           ("vmid", acct["vmid"])) if not v]


# ───────────────────────── HTTP ─────────────────────────

def request(path, cookie, data=None, ajax=False, timeout=45):
    h = {"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"}
    if cookie:
        h["Cookie"] = cookie
    if ajax:
        h.update({"Accept": "application/json",
                  "X-Requested-With": "XMLHttpRequest",
                  "Referer": BASE + "/"})
    if data is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded"
        h["Origin"] = BASE
        h["Referer"] = BASE + "/"
    req = urllib.request.Request(BASE + path, data=data, headers=h,
                                 method="POST" if data is not None else "GET")
    # 走代理 opener（未启用代理时即默认 opener，行为不变）
    return socks_proxy.get_opener().open(req, timeout=timeout)


def get_html(path, cookie):
    return request(path, cookie).read().decode("utf-8", "ignore")


def get_json(path, cookie):
    return json.loads(request(path, cookie, ajax=True).read().decode())


# ───────────────────────── 面板操作 ─────────────────────────

def panel_csrf(cookie):
    dash = get_html("/", cookie)
    m = re.search(r'CSRF\s*=\s*["\']([^"\']+)', dash)
    if not m:
        m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', dash)
    return (m.group(1) if m else None), dash


def parse_expiry(html):
    """返回 (到期日字符串, 剩余天数 或 None)"""
    e = re.search(r'Échéance le\s*([0-9]{2}/[0-9]{2}/[0-9]{4})', html)
    j = re.search(r'(\d+)\s*jours?\s*restants?', html)
    return (e.group(1) if e else None), (int(j.group(1)) if j else None)


def session_ok(cookie):
    """cookie 是否仍然有效"""
    try:
        dash = get_html("/", cookie)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, type(e).__name__
    if 'name="identifier"' in dash or "Connexion" in dash[:2000]:
        return False, "session 已过期（返回登录页）"
    if "Tableau de bord" not in dash and "Mes services" not in dash:
        return False, "未识别为面板页面"
    return True, "ok"


def renew(cookie, csrf, acct):
    body = urllib.parse.urlencode(
        {"csrf_token": csrf, "type": acct["type"], "id": acct["vmid"]}).encode()
    r = request("/services/renew", cookie, data=body, timeout=120)
    html = r.read().decode("utf-8", "ignore")
    m = re.search(r'"message"\s*:\s*"([^"]*)"', html)
    msg = m.group(1).encode().decode("unicode_escape") if m else ""
    ok = "renouvel" in msg.lower()
    return ok, msg or f"HTTP {r.status}"


def power(cookie, csrf, acct, signal):
    body = urllib.parse.urlencode(
        {"vmid": acct["vmid"], "signal": signal, "csrf_token": csrf}).encode()
    r = request("/app/services/vps-power.php", cookie, data=body, ajax=True, timeout=90)
    return json.loads(r.read().decode())


def vps_status(cookie, acct):
    return get_json(f"/app/services/vps-stats.php?id={acct['vmid']}", cookie)


# ───────────────────────── cookie 刷新 ─────────────────────────
#
# ★ 2026-10 站点改版导致登录页 GET 返回【残缺 HTML】（无 </html>、无
#   <form>/<input>，稳定停在同一长度），表单元素根本不存在 →
#   page.fill 超时。这是服务端问题，脚本只能重试等它恢复。
#   同时确认：① 登录是两步流程（identifier → #goToPassword → password）
#            ② POST /login 需要 csrf_token
#            ③ /login 有速率限制，连续请求返回 429
#   因此这里加：重试 + 指数退避 + 429 识别 + CSRF 注入 + 诊断输出。

LOGIN_ATTEMPTS = int(os.environ.get("NEO_LOGIN_ATTEMPTS", "4"))
LOGIN_BACKOFF = [int(x) for x in
                 os.environ.get("NEO_LOGIN_BACKOFF", "15,45,120,300").split(",")]


def _backoff(attempt):
    """第 attempt 次失败（1 起）后应等待的秒数"""
    i = min(attempt - 1, len(LOGIN_BACKOFF) - 1)
    return LOGIN_BACKOFF[i] if LOGIN_BACKOFF else 30


def page_diag(page):
    """登录失败时抓页面状态，便于判断是站点异常还是选择器问题"""
    d = {}
    try:
        html = page.content()
        d["url"] = page.url
        d["title"] = page.title()[:60]
        d["html_len"] = len(html)
        d["html_closed"] = "</html>" in html
        d["has_identifier"] = 'name="identifier"' in html
        d["has_password"] = 'name="password"' in html
        d["inputs"] = page.evaluate("() => document.querySelectorAll('input').length")
        d["cap_widget"] = page.evaluate("() => document.querySelectorAll('cap-widget').length")
        d["rate_limited"] = "Trop de requ" in html
    except Exception as e:
        d["error"] = f"{type(e).__name__}: {str(e)[:80]}"
    return d


def diag_str(d):
    if not d:
        return "(无)"
    if "error" in d and len(d) == 1:
        return d["error"]
    parts = [f"{k}={d[k]}" for k in ("url", "html_len", "inputs", "cap_widget",
                                     "html_closed", "has_identifier") if k in d]
    return " ".join(parts)


def read_csrf(page):
    """从页面读 csrf_token（隐藏 input / meta / JS 变量都试）"""
    try:
        return page.evaluate("""() => {
            const i = document.querySelector('input[name="csrf_token"]');
            if (i && i.value) return i.value;
            const m = document.querySelector('meta[name="csrf-token"]');
            if (m && m.content) return m.content;
            if (window.CSRF_TOKEN) return window.CSRF_TOKEN;
            if (typeof window.CSRF === 'string') return window.CSRF;
            return '';
        }""") or ""
    except Exception:
        return ""


def refresh_cookie(username, password):
    """无头浏览器登录，返回新的 Cookie 头字符串（含重试/退避/CSRF）"""
    if not password:
        return None, "缺少密码，无法自动刷新 cookie"

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return None, f"playwright 不可用: {type(e).__name__}"

    last_msg = "未尝试"
    last_diag = {}

    for attempt in range(1, LOGIN_ATTEMPTS + 1):
        if attempt > 1:
            wait = _backoff(attempt - 1)
            log(f"  第 {attempt}/{LOGIN_ATTEMPTS} 次登录尝试（等待 {wait}s）…")
            time.sleep(wait)
        else:
            log(f"  第 {attempt}/{LOGIN_ATTEMPTS} 次登录尝试…")

        cand, msg, diag = _login_once(sync_playwright, username, password)
        last_msg, last_diag = msg, diag

        if cand and "__Host-NH-Remember" in cand:
            if attempt > 1:
                log(f"  ✅ 第 {attempt} 次尝试成功")
            hdr = "; ".join(f"{k}={v}" for k, v in cand.items())
            return hdr, "ok"

        log(f"  ✗ 尝试 {attempt} 失败: {msg}")
        if diag:
            log(f"    页面状态: {diag_str(diag)}")
        # ★ 站点页面残缺 / 表单缺失 → 只能等它恢复，继续重试
        if diag.get("rate_limited"):
            log("    站点返回 429（速率限制），拉长退避")
            time.sleep(30)

    hint = ""
    if last_diag and not last_diag.get("has_identifier", True):
        hint = ("（登录页表单缺失 —— 站点 GET /login 返回残缺 HTML，"
                "非本脚本问题；等站点恢复）")
    return None, f"{LOGIN_ATTEMPTS} 次尝试均失败，最后错误: {last_msg}{hint}"


def _login_once(sync_playwright, username, password):
    """单次登录尝试。返回 (cookie_dict 或 None, 消息, 诊断 dict)"""
    cand = {}
    diag = {}
    launch_errors = []
    px = socks_proxy.playwright_proxy()
    with sync_playwright() as p:
        browser = None
        for kwargs in ({"channel": "chrome"}, {}):
            try:
                launch_kw = dict(headless=True,
                                 args=["--disable-blink-features=AutomationControlled",
                                       "--no-sandbox", "--disable-dev-shm-usage"],
                                 **kwargs)
                if px:
                    launch_kw["proxy"] = px
                browser = p.chromium.launch(**launch_kw)
                log(f"浏览器: {kwargs.get('channel') or 'bundled chromium'}"
                    f"{'（经代理）' if px else ''}")
                break
            except Exception as e:
                launch_errors.append(f"{kwargs.get('channel') or 'chromium'}: "
                                     f"{type(e).__name__}")
                browser = None
        if browser is None:
            return None, f"浏览器启动失败 ({', '.join(launch_errors)})", {}

        ctx = browser.new_context(
            user_agent=UA, viewport={"width": 1920, "height": 1080}, locale="fr-FR")
        page = ctx.new_page()
        try:
            page.goto(BASE + "/login", timeout=60000, wait_until="domcontentloaded")

            # ★ 先等表单出现 —— 站点残缺 HTML 时这里就会失败（不再盲填）
            try:
                page.wait_for_selector('input[name="identifier"]', timeout=45000)
            except Exception:
                diag = page_diag(page)
                return None, "登录表单未渲染（input[name=identifier] 不存在）", diag

            page.fill('input[name="identifier"]', username)
            page.click("#goToPassword")
            page.wait_for_timeout(1500)

            # 第二步：密码框
            try:
                page.wait_for_selector('input[name="password"]', timeout=25000)
            except Exception:
                diag = page_diag(page)
                return None, "第二步密码框未出现（#goToPassword 跳转失败）", diag

            page.evaluate("() => { const w=document.getElementById('cap-login'); if (w) w.solve(); }")

            token = ""
            for _ in range(30):
                page.wait_for_timeout(2000)
                token = page.evaluate(
                    "() => { const e=document.querySelector('input[name=\"cap-token\"]');"
                    " return e ? e.value : ''; }")
                if token and len(token) > 20:
                    break
            if not token:
                diag = page_diag(page)
                return None, "验证码求解超时（cap-token 未生成）", diag

            page.fill('input[name="password"]', password)

            # ★ POST /login 需要 csrf_token；表单里没有就注入
            csrf = read_csrf(page)
            if csrf:
                log(f"  csrf_token: 已取到（{len(csrf)} 字符）")
            else:
                log("  csrf_token: 页面未提供（尝试直接提交）")
            page.evaluate("""(csrf) => {
                const f = document.querySelector('input[name="password"]').form;
                if (csrf && !f.querySelector('input[name="csrf_token"]')) {
                    const h = document.createElement('input');
                    h.type='hidden'; h.name='csrf_token'; h.value=csrf;
                    f.appendChild(h);
                }
                f.submit();
            }""", csrf)

            try:
                page.wait_for_url(re.compile(r"dash\.neoheberg\.fr/(\?.*)?$"),
                                  timeout=45000)
            except Exception:
                pass
            page.wait_for_timeout(2000)

            for c in ctx.cookies():
                if c["name"].startswith("__Host-NH"):
                    cand[c["name"]] = c["value"]

            if "__Host-NH-Remember" not in cand:
                diag = page_diag(page)
                # 站点是否给了明确的错误提示
                try:
                    err = page.evaluate("""() => {
                        const a = document.querySelector('[role=alert], .alert-card');
                        return a ? a.innerText.trim().slice(0,160) : '';
                    }""")
                    if err:
                        diag["site_error"] = re.sub(r"\s+", " ", err)
                except Exception:
                    pass
                return None, f"未取得 remember cookie（拿到 {list(cand)}）", diag
        finally:
            browser.close()

    return cand, "ok", diag


# ───────────────────────── Telegram ─────────────────────────

def notify(text):
    tok = (os.environ.get("TG_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TG_CHAT_ID") or "").strip()
    if not tok or not chat:
        log("TG 未配置，跳过通知")
        return
    try:
        data = urllib.parse.urlencode(
            {"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
        r = urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        resp = json.loads(urllib.request.urlopen(r, timeout=30).read().decode())
        log(f"TG 通知: {'ok' if resp.get('ok') else resp}")
    except Exception as e:
        log(f"TG 通知失败: {type(e).__name__}: {str(e)[:120]}")


# ───────────────────────── 单账号流程 ─────────────────────────

def process_account(acct, preseed_cookie=""):
    """处理一个账号。返回结果 dict（含 actions / hard_fail）。"""
    r = {"name": acct["name"], "actions": [], "expiry": None, "days": None,
         "status": "unknown", "hard_fail": False, "site_issue": False}

    cookie = ""
    if preseed_cookie:
        ok, why = session_ok(preseed_cookie)
        log(f"预置 cookie 检查: {'有效' if ok else '失效'} — {why}")
        if ok:
            cookie = preseed_cookie
            r["actions"].append("使用预置 cookie")

    # 1. 登录
    if not cookie:
        log("无头浏览器登录…")
        cookie, msg = refresh_cookie(acct["user"], acct["password"])
        if not cookie:
            r["hard_fail"] = True
            if "表单缺失" in msg or "残缺 HTML" in msg:
                r["site_issue"] = True
                r["actions"].append(f"登录失败（站点侧问题）: {msg}")
            else:
                r["actions"].append(f"登录失败: {msg}")
            log(f"登录失败: {msg}")
            return r
        log("登录成功")
        r["actions"].append("登录成功")

    # 2. 到期检查
    try:
        csrf, dash = panel_csrf(cookie)
    except Exception as e:
        r["hard_fail"] = True
        r["actions"].append(f"面板读取失败 {type(e).__name__}: {str(e)[:120]}")
        log(r["actions"][-1])
        return r

    exp, days = parse_expiry(dash)
    r["expiry"], r["days"] = exp, days
    log(f"到期日: {exp}  剩余: {days} 天")
    if csrf is None:
        r["hard_fail"] = True
        r["actions"].append("未取到 CSRF token")
        log(r["actions"][-1])
        return r

    # 3. 续期
    if days is not None and days <= acct["renew_days"]:
        try:
            rok, rmsg = renew(cookie, csrf, acct)
            log(f"续期: {rok} — {rmsg}")
            r["actions"].append(f"续期{'成功' if rok else '失败'}: {rmsg}")
            if not rok:
                r["hard_fail"] = True
        except Exception as e:
            log(f"续期异常: {type(e).__name__}: {str(e)[:150]}")
            r["actions"].append(f"续期异常 {type(e).__name__}")
            r["hard_fail"] = True
    else:
        log(f"未到续期阈值（{acct['renew_days']} 天），跳过")
        r["actions"].append(f"续期跳过（剩 {days} 天）")

    # 4. 容器状态 + 开机守护
    try:
        st = vps_status(cookie, acct)
        r["status"] = (st.get("status") or "unknown").lower()
        log(f"容器状态: {r['status']}  uptime={st.get('uptime')}s  "
            f"RAM={st.get('ram',{}).get('percent')}%")
    except Exception as e:
        log(f"状态查询失败: {type(e).__name__}: {str(e)[:120]}")

    if r["status"] in ("stopped", "halted", "shutdown"):
        try:
            res = power(cookie, csrf, acct, "start")
            log(f"开机指令: {res}")
            r["actions"].append("检测到停机 → 已下发开机")
            time.sleep(45)
            try:
                st2 = vps_status(cookie, acct)
                s2 = (st2.get("status") or "?").lower()
                r["status"] = s2
                r["actions"].append(f"开机后状态: {s2}")
                log(f"开机 45s 后: {s2}")
            except Exception:
                pass
        except Exception as e:
            log(f"开机失败: {type(e).__name__}: {str(e)[:150]}")
            r["actions"].append(f"开机失败 {type(e).__name__}")
            r["hard_fail"] = True
    elif r["status"] in ("starting", "stopping", "restarting"):
        log("容器处于过渡状态，不干预")
        r["actions"].append(f"容器过渡中({r['status']})，跳过")
    elif r["status"] == "unknown":
        log("状态未知（查询失败），不做干预")
        r["actions"].append("容器状态未知（查询失败）")
    else:
        log("容器运行中，无需开机")
        r["actions"].append(f"容器正常({r['status']})")

    # 5. 重新读取到期日（续期后）
    try:
        _, dash2 = panel_csrf(cookie)
        exp2, days2 = parse_expiry(dash2)
        r["expiry"], r["days"] = exp2, days2
    except Exception:
        pass

    return r


# ───────────────────────── 汇总通知 ─────────────────────────

def build_summary(results, disabled, config_errors, proxy_desc):
    total = len(results) + len(disabled) + len(config_errors)
    bad = [r for r in results if r["hard_fail"]]
    ok_n = len(results) - len(bad)
    site = [r for r in results if r["site_issue"]]
    icon = "✅" if not bad and not config_errors else "⚠️"

    lines = [f"{icon} NeoHeberg 多账号续期（成功 {ok_n}/{total}）"]
    for r in results:
        mark = "⚠️" if r["hard_fail"] else "•"
        lines.append(f"{mark} [{r['name']}] {'; '.join(r['actions']) or '无动作'}")
        if r["expiry"] or r["days"] is not None:
            lines.append(f"    到期 {r['expiry']}（剩 {r['days']} 天）"
                         f"｜容器 {r['status']}")
    for name, miss in config_errors:
        lines.append(f"⚠️ [{name}] 配置缺失: {', '.join(miss)}（已跳过）")
    if disabled:
        lines.append(f"（已禁用: {', '.join(disabled)}）")
    if site:
        lines.append(f"提示: {', '.join(r['name'] for r in site)} 是站点侧问题"
                     f"（登录页返回残缺 HTML），等站点恢复后自动重试")
    lines.append(f"代理: {proxy_desc}")
    if RUN_URL:
        lines.append(RUN_URL)
    return "\n".join(lines)


def run_selftest(username):
    """自检：验证无头浏览器能启动并解出 cap-token（不做登录，不动 cookie）"""
    log("── 自检模式：测试无头浏览器 + Cap.js 求解 ──")
    # 代理与主流程保持一致：先 init，再取 playwright proxy
    try:
        socks_proxy.init()
        log(f"代理: {socks_proxy.describe()}")
    except Exception as e:
        log(f"代理初始化异常: {type(e).__name__}: {str(e)[:120]}")
    from playwright.sync_api import sync_playwright
    t0 = time.time()
    px = socks_proxy.playwright_proxy()
    try:
        with sync_playwright() as p:
            browser = None
            for kwargs in ({"channel": "chrome"}, {}):
                try:
                    lk = dict(headless=True,
                              args=["--disable-blink-features=AutomationControlled",
                                    "--no-sandbox", "--disable-dev-shm-usage"], **kwargs)
                    if px:
                        lk["proxy"] = px
                    browser = p.chromium.launch(**lk)
                    log(f"浏览器启动: {kwargs.get('channel') or 'bundled chromium'} "
                        f"({time.time()-t0:.1f}s){'（经代理）' if px else ''}")
                    break
                except Exception as e:
                    log(f"  {kwargs.get('channel') or 'chromium'} 启动失败: {type(e).__name__}")
                    browser = None
            if browser is None:
                raise RuntimeError("无可用浏览器")

            ctx = browser.new_context(user_agent=UA, viewport={"width": 1920, "height": 1080},
                                      locale="fr-FR")
            page = ctx.new_page()
            page.goto(BASE + "/login", timeout=60000, wait_until="domcontentloaded")

            # ★ 与主流程一致：先等表单渲染，再判断站点是否正常
            try:
                page.wait_for_selector('input[name="identifier"]', timeout=45000)
            except Exception:
                d = page_diag(page)
                log(f"❌ 登录表单未渲染 —— {diag_str(d)}")
                log("   若 html_closed=False / inputs=0：站点 GET /login 返回残缺 HTML（服务端问题）")
                browser.close()
                return 1

            page.fill('input[name="identifier"]', username)
            page.click("#goToPassword")
            page.wait_for_timeout(1500)
            try:
                page.wait_for_selector('input[name="password"]', timeout=25000)
            except Exception:
                log("❌ 第二步密码框未出现（#goToPassword 跳转失败）")
                browser.close()
                return 1

            page.evaluate("() => { const w=document.getElementById('cap-login'); if (w) w.solve(); }")
            token = ""
            for _ in range(30):
                page.wait_for_timeout(2000)
                token = page.evaluate(
                    "() => { const e=document.querySelector('input[name=\"cap-token\"]');"
                    " return e ? e.value : ''; }")
                if token and len(token) > 20:
                    break
            csrf = read_csrf(page)
            log(f"csrf_token: {'取到 ' + str(len(csrf)) + ' 字符' if csrf else '页面未提供'}")
            browser.close()

        if token and len(token) > 20:
            log(f"✅ 自检通过: cap-token 长度 {len(token)}（耗时 {time.time()-t0:.1f}s）")
            return 0
        log("❌ 自检失败: 未取到 cap-token")
        return 1
    except Exception as e:
        log(f"❌ 自检异常: {type(e).__name__}: {str(e)[:250]}")
        return 1


# ───────────────────────── 主流程 ─────────────────────────

def _arg_value(flag):
    """取 --flag value 形式的参数值"""
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return ""


def main():
    accounts, src = load_accounts()

    if "--list" in sys.argv:
        log(f"账号来源: {src}")
        for a in accounts:
            log(f"  [{a['name']}] user={mask(a['user'])} vmid={mask(a['vmid'])} "
                f"type={a['type']} renew_days={a['renew_days']} "
                f"enabled={a['enabled']}")
        return 0

    if "--selftest" in sys.argv:
        uname = next((a["user"] for a in accounts if a["user"]), "") \
            or os.environ.get("NEO_USER", "") or "selftest"
        return run_selftest(uname)

    if not accounts:
        log("未配置任何账号：请设置 NEO_ACCOUNTS（JSON）或 NEO_ACCOUNTS_FILE，"
            "或旧版 NEO_USER/NEO_PASSWORD/NEO_VMID")
        notify("❌ NeoHeberg: 未配置任何账号")
        return 1

    only = _arg_value("--only")
    if only:
        accounts = [a for a in accounts if a["name"] == only]
        if not accounts:
            log(f"--only {only} 未匹配到账号")
            return 1

    log(f"账号来源: {src}，共 {len(accounts)} 个"
        f"{f'（--only {only}）' if only else ''}")

    # 0. 启用代理（固定出口 IP，降低风控；未配置则直连）
    try:
        socks_proxy.init()
        log(f"代理: {socks_proxy.describe()}")
    except Exception as e:
        log(f"代理初始化异常: {type(e).__name__}: {str(e)[:120]}")
        notify(f"❌ NeoHeberg: 代理初始化失败 {type(e).__name__}: {str(e)[:150]}")
        return 1
    proxy_desc = socks_proxy.describe()

    # 预置 cookie 仅在单账号时使用（多账号共用一份 cookie 没有意义）
    preseed = (os.environ.get("NEO_COOKIE") or "").strip()
    if preseed and len(accounts) > 1:
        log("检测到 NEO_COOKIE 但账号数 > 1，忽略（每个账号都会重新登录）")
        preseed = ""

    results, disabled, config_errors = [], [], []
    processed = 0
    for acct in accounts:
        CURRENT["tag"] = acct["name"]
        if not acct["enabled"]:
            log("已禁用，跳过")
            disabled.append(acct["name"])
            continue

        miss = missing_fields(acct)
        if miss:
            log(f"配置缺失: {', '.join(miss)}，跳过")
            config_errors.append((acct["name"], miss))
            continue

        if processed:
            log(f"等待 {ACCOUNT_DELAY}s 后处理下一个账号…")
            time.sleep(ACCOUNT_DELAY)
        processed += 1

        log(f"处理账号 [{acct['name']}] user={mask(acct['user'])} "
            f"实例={mask(acct['vmid'])} 续期阈值={acct['renew_days']}天")
        try:
            results.append(process_account(acct, preseed))
        except Exception as e:
            log(f"账号处理异常: {type(e).__name__}: {str(e)[:180]}")
            results.append({"name": acct["name"],
                            "actions": [f"未捕获异常 {type(e).__name__}"],
                            "expiry": None, "days": None, "status": "unknown",
                            "hard_fail": True, "site_issue": False})

    CURRENT["tag"] = ""
    notify(build_summary(results, disabled, config_errors, proxy_desc))

    log("完成")
    # 续期/开机/配置类失败让 workflow 标红，便于发现
    return 1 if any(r["hard_fail"] for r in results) or config_errors else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "ignore")[:200]
        log(f"HTTPError {e.code}: {msg}")
        notify(f"❌ NeoHeberg 任务异常：HTTP {e.code}\n{msg[:180]}")
        sys.exit(1)
    except Exception as e:
        log(f"未捕获异常: {type(e).__name__}: {e}")
        notify(f"❌ NeoHeberg 任务异常：{type(e).__name__}: {str(e)[:180]}")
        sys.exit(1)
