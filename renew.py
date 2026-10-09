#!/usr/bin/env python3
"""
NeoHeberg VPS 多账号自动续期 + 开机守护

每次运行（GitHub Actions 每日一次）对每个账号依次执行：
  1. 无头浏览器登录面板（自动解 Cap.js 验证码），拿到本次会话 cookie
  2. 检查到期日，剩余天数 <= RENEW_DAYS 则续期（+31 天）
  3. 检查容器状态，已停止则开机
  4. 每个账号单独发一条 Telegram 通知

环境变量（账号类均按行一一对应，一行 = 一个账号/实例）：
  NEO_USER       面板登录账号，每行一个
  NEO_PASSWORD   面板密码，每行一个
  NEO_VMID       容器实例 ID，每行一个
  NEO_TYPE       实例类型，每行一个（可选，默认 vps）
  NEO_LABEL      显示名，每行一个（可选，默认用 VMID）
  SELECTED_ACCOUNTS  手动触发时选择的账号序号，如 1,3 或 2-4，留空=全部
  TG_BOT_TOKEN   Telegram bot token（可留空则不通知）
  TG_CHAT_ID     Telegram chat id
  RENEW_DAYS     剩余多少天时续期，默认 14
"""
import os
import re
import sys
import json
import time
import urllib.request
import urllib.parse
import urllib.error

BASE = "https://dash.neoheberg.fr"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

RENEW_DAYS = int(os.environ.get("RENEW_DAYS", "14"))
TRIGGER = os.environ.get("GITHUB_EVENT_NAME", "local")
RUN_URL = ""
if os.environ.get("GITHUB_RUN_ID"):
    RUN_URL = (f"{os.environ.get('GITHUB_SERVER_URL','https://github.com')}/"
               f"{os.environ.get('GITHUB_REPOSITORY','')}/actions/runs/"
               f"{os.environ['GITHUB_RUN_ID']}")


def mask(s, show=2):
    """日志脱敏：只留首尾各 show 位"""
    s = str(s)
    if not s:
        return "(空)"
    if len(s) <= show * 2:
        return "*" * len(s)
    return s[:show] + "*" * (len(s) - show * 2) + s[-show:]


def mask_user(account):
    """账号脱敏：邮箱保留用户名前2位和后2位，其余用 **** 代替。"""
    if "@" in account:
        name, domain = account.split("@", 1)
        if len(name) > 4:
            return f"{name[:2]}****{name[-2:]}@{domain}"
        return f"{name}@{domain}"
    return mask(account)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ───────────────────────── 账号配置解析 ─────────────────────────

def _split_lines(raw):
    """把多行 / 逗号 / 分号分隔的配置拆成列表，自动去空行和首尾空格。"""
    if not raw:
        return []
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    for sep in (",", ";", "|"):
        if "\n" not in text and sep in text:
            text = text.replace(sep, "\n")
    return [line.strip() for line in text.split("\n") if line.strip()]


def _parse_selected(raw, total):
    """解析手动触发填写的账号序号（如 1,3 或 2-4），返回 0 起始的下标列表。

    序号非法或超出范围时抛 ValueError，避免误跑全部账号。
    """
    text = raw.replace("，", ",").replace("、", ",").replace("－", "-").replace(" ", "")
    if not text:
        return None  # 留空 = 全部

    picked = set()
    for part in text.split(","):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise ValueError(f"区间格式不正确: {part}")
            lo, hi = int(a), int(b)
            if lo > hi:
                lo, hi = hi, lo
            picked.update(range(lo, hi + 1))
        elif part.isdigit():
            picked.add(int(part))
        else:
            raise ValueError(f"序号格式不正确: {part}")

    if 0 in picked:
        raise ValueError("账号序号从 1 开始")
    out_of_range = [p for p in sorted(picked) if p > total]
    if out_of_range:
        raise ValueError(f"序号超出范围: {out_of_range}（当前共 {total} 个账号）")
    return [p - 1 for p in sorted(picked)]


def load_accounts():
    """从 Secrets 载入多账号列表，返回 [{user, password, vmid, type, label}, ...]。"""
    users = _split_lines(os.environ.get("NEO_USER", ""))
    passwords = _split_lines(os.environ.get("NEO_PASSWORD", ""))
    vmids = _split_lines(os.environ.get("NEO_VMID", ""))
    types = _split_lines(os.environ.get("NEO_TYPE", ""))
    labels = _split_lines(os.environ.get("NEO_LABEL", ""))

    if not users or not passwords or not vmids:
        print("❌ 未配置 NEO_USER / NEO_PASSWORD / NEO_VMID（Secrets，每行一个）")
        return []

    n = len(users)
    for name, lst in (("NEO_PASSWORD", passwords), ("NEO_VMID", vmids)):
        if len(lst) != n:
            print(f"❌ 数量不匹配：NEO_USER {n} 行，{name} {len(lst)} 行。"
                  "请确保每行一一对应。")
            return []

    def pad(lst, default):
        """行数不足时用默认值补齐，多出则截断。"""
        lst = lst[:n]
        return lst + [default] * (n - len(lst))

    types = pad(types, "vps")
    labels = pad(labels, "")

    accounts = [
        {"user": users[i], "password": passwords[i], "vmid": vmids[i],
         "type": types[i] or "vps", "label": labels[i] or vmids[i]}
        for i in range(n)
    ]

    # 手动触发时按序号筛选（renew.yml 传入，留空 = 全部）
    selected_raw = (os.environ.get("SELECTED_ACCOUNTS") or "").strip()
    if selected_raw:
        try:
            idx_list = _parse_selected(selected_raw, len(accounts))
        except ValueError as e:
            print(f"❌ 账号序号解析失败: {e}")
            return []
        if idx_list is not None:
            accounts = [accounts[i] for i in idx_list]
            preview = ", ".join(mask_user(a["user"]) for a in accounts)
            print(f"🎯 手动选择账号 [{selected_raw}]: {preview}")

    print(f"👥 已载入 {len(accounts)} 个账号")
    return accounts


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
    return urllib.request.urlopen(req, timeout=timeout)


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


def renew(cookie, csrf, vmid, type_="vps"):
    body = urllib.parse.urlencode(
        {"csrf_token": csrf, "type": type_, "id": vmid}).encode()
    r = request("/services/renew", cookie, data=body, timeout=120)
    html = r.read().decode("utf-8", "ignore")
    m = re.search(r'"message"\s*:\s*"([^"]*)"', html)
    msg = m.group(1).encode().decode("unicode_escape") if m else ""
    ok = "renouvel" in msg.lower()
    return ok, msg or f"HTTP {r.status}"


def power(cookie, csrf, vmid, signal):
    body = urllib.parse.urlencode(
        {"vmid": vmid, "signal": signal, "csrf_token": csrf}).encode()
    r = request("/app/services/vps-power.php", cookie, data=body, ajax=True, timeout=90)
    return json.loads(r.read().decode())


def vps_status(cookie, vmid):
    return get_json(f"/app/services/vps-stats.php?id={vmid}", cookie)


# ───────────────────────── 无头浏览器登录 ─────────────────────────

def launch_browser(p):
    """启动无头 Chromium：优先系统 Chrome，CI 里回落到 playwright 自带内核。"""
    errors = []
    for kwargs in ({"channel": "chrome"}, {}):
        try:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled",
                      "--no-sandbox", "--disable-dev-shm-usage"],
                **kwargs)
            log(f"浏览器: {kwargs.get('channel') or 'bundled chromium'}")
            return browser, None
        except Exception as e:
            errors.append(f"{kwargs.get('channel') or 'chromium'}: {type(e).__name__}")
    return None, f"浏览器启动失败 ({', '.join(errors)})"


def login_one(browser, user, password):
    """用独立 context 登录一个账号，返回 (Cookie 头字符串, 说明)。

    context 之间 cookie 隔离，因此多个账号不会互相串会话。
    """
    cand = {}
    ctx = browser.new_context(user_agent=UA, viewport={"width": 1920, "height": 1080},
                              locale="fr-FR")
    try:
        page = ctx.new_page()
        page.goto(BASE + "/login", timeout=60000, wait_until="domcontentloaded")
        page.fill('input[name="identifier"]', user)
        page.click("#goToPassword")
        page.wait_for_timeout(1500)
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
            return None, "验证码求解超时"

        page.fill('input[name="password"]', password)
        page.evaluate("""() => {
            const f = document.querySelector('input[name="password"]').form;
            f.submit();
        }""")
        try:
            page.wait_for_url(re.compile(r"dash\.neoheberg\.fr/(\?.*)?$"), timeout=45000)
        except Exception:
            pass
        page.wait_for_timeout(2000)

        for c in ctx.cookies():
            if c["name"].startswith("__Host-NH"):
                cand[c["name"]] = c["value"]
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        ctx.close()

    if "__Host-NH-Remember" not in cand:
        return None, f"登录未取得 remember cookie（拿到 {list(cand)}）"
    return "; ".join(f"{k}={v}" for k, v in cand.items()), "ok"


# ───────────────────────── Telegram ─────────────────────────

def notify(text):
    tok = os.environ.get("TG_BOT_TOKEN")
    chat = os.environ.get("TG_CHAT_ID")
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


def notify_block(result, index, total):
    """每个账号单独一条通知（无汇总消息）。

    账号与实例沿用与日志一致的脱敏写法；若想直接看明文，
    把 mask_user / mask 换成 result['user'] / result['label'] 即可。
    """
    sep = "—" * 10
    days = result.get("days")
    expiry = result.get("expiry") or "未知"
    lines = [
        f"🇫🇷 NeoHeberg 续期 [{index}/{total}]",
        sep,
        f"{result['icon']} {result['status']}",
        f"👤 续期账户: {mask_user(result['user'])}",
        f"🖥️ 实例: {mask(result['vmid'])}",
        f"📅 到期: {expiry}（剩 {days if days is not None else '?'} 天）",
        f"📦 容器: {result.get('container', 'unknown')}",
        sep,
    ]
    if RUN_URL:
        lines.append(RUN_URL)
    notify("\n".join(lines))


# ───────────────────────── 单账号流程 ─────────────────────────

class _Abort(Exception):
    """账号内部提前结束（已写入 result，不再继续面板操作）。"""


def _renew_and_watch(cookie, acct, result):
    vmid, type_ = acct["vmid"], acct["type"]

    csrf, dash = panel_csrf(cookie)
    if csrf is None:
        result.update(icon="❌", status="未取到 CSRF token")
        return

    exp, days = parse_expiry(dash)
    result["expiry"], result["days"] = exp, days
    log(f"到期日: {exp}  剩余: {days} 天")

    # 1) 续期
    if days is None:
        result.update(icon="⚠️", ok=True, status="未识别到期日，跳过续期")
        log("未解析到到期日，跳过续期")
    elif days <= RENEW_DAYS:
        rok, rmsg = renew(cookie, csrf, vmid, type_)
        log(f"续期: {rok} — {rmsg}")
        result.update(ok=rok, icon="✅" if rok else "❌",
                      status="续期成功" if rok else f"续期失败: {rmsg}", alert=rmsg)
    else:
        log(f"未到续期阈值（{RENEW_DAYS} 天），跳过")
        result.update(ok=True, icon="ℹ️", status=f"未到续期时间（剩 {days} 天）")

    # 2) 容器保活
    status = "unknown"
    try:
        st = vps_status(cookie, vmid)
        status = (st.get("status") or "unknown").lower()
        log(f"容器状态: {status}  uptime={st.get('uptime')}s  "
            f"RAM={st.get('ram', {}).get('percent')}%")
    except Exception as e:
        log(f"状态查询失败: {type(e).__name__}: {str(e)[:120]}")
    result["container"] = status

    if status in ("stopped", "halted", "shutdown"):
        try:
            res = power(cookie, csrf, vmid, "start")
            log(f"开机指令: {res}")
            result["status"] += " / 检测到停机 → 已开机"
            time.sleep(45)
            try:
                s2 = (vps_status(cookie, vmid).get("status") or "?").lower()
                result["container"] = s2
                log(f"开机 45s 后: {s2}")
            except Exception:
                pass
        except Exception as e:
            log(f"开机失败: {type(e).__name__}: {str(e)[:150]}")
            result.update(icon="❌", ok=False,
                          status=result["status"] + f" / 开机失败 {type(e).__name__}")
    elif status in ("starting", "stopping", "restarting"):
        log("容器处于过渡状态，不干预")
        result["status"] += f" / 容器过渡中({status})"
    else:
        log("容器运行中，无需开机")

    # 3) 续期后重读到期日
    try:
        _, dash2 = panel_csrf(cookie)
        exp2, days2 = parse_expiry(dash2)
        result["expiry"] = exp2 or exp
        result["days"] = days2 if days2 is not None else days
    except Exception:
        pass


def process_account(browser, acct, index, total, cookie_cache):
    """处理单个账号：登录 -> 续期 -> 保活 -> 通知，返回结果字典。"""
    log("=" * 46)
    log(f"账号 [{index}/{total}] {mask_user(acct['user'])}  实例 {mask(acct['vmid'])}")
    log("=" * 46)

    result = {"user": acct["user"], "vmid": acct["vmid"], "label": acct["label"],
              "ok": False, "icon": "❌", "status": "未执行", "expiry": None,
              "days": None, "container": "unknown", "alert": ""}

    try:
        cookie = cookie_cache.get(acct["user"])
        if cookie:
            log("复用本账号已登录的 cookie（同一账号的多个实例只登录一次）")
        else:
            log("无头浏览器登录…")
            cookie, msg = login_one(browser, acct["user"], acct["password"])
            if not cookie:
                log(f"登录失败: {msg}")
                result.update(icon="❌", status=f"登录失败: {msg}")
                raise _Abort()
            cookie_cache[acct["user"]] = cookie
            log("登录成功")

        _renew_and_watch(cookie, acct, result)
    except _Abort:
        pass
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:150]
        log(f"HTTPError {e.code}: {body}")
        result.update(icon="❌", ok=False, status=f"HTTP {e.code}")
    except Exception as e:
        log(f"账号处理异常: {type(e).__name__}: {str(e)[:150]}")
        result.update(icon="❌", ok=False, status=f"处理异常 {type(e).__name__}")

    notify_block(result, index, total)
    return result


# ───────────────────────── 自检 / 入口 ─────────────────────────

def selftest():
    """自检：验证无头浏览器能启动并解出 cap-token（不做登录，不动状态）"""
    log("── 自检模式：测试无头浏览器 + Cap.js 求解 ──")
    users = _split_lines(os.environ.get("NEO_USER", ""))
    if not users:
        log("❌ 缺少 NEO_USER")
        return 1

    from playwright.sync_api import sync_playwright
    t0 = time.time()
    try:
        with sync_playwright() as p:
            browser, err = launch_browser(p)
            if browser is None:
                log(f"❌ {err}")
                return 1
            try:
                ctx = browser.new_context(user_agent=UA,
                                          viewport={"width": 1920, "height": 1080},
                                          locale="fr-FR")
                page = ctx.new_page()
                page.goto(BASE + "/login", timeout=60000, wait_until="domcontentloaded")
                page.fill('input[name="identifier"]', users[0])
                page.click("#goToPassword")
                page.wait_for_timeout(1500)
                page.evaluate("() => { const w=document.getElementById('cap-login'); if (w) w.solve(); }")
                token = ""
                for _ in range(30):
                    page.wait_for_timeout(2000)
                    token = page.evaluate(
                        "() => { const e=document.querySelector('input[name=\"cap-token\"]');"
                        " return e ? e.value : ''; }")
                    if token and len(token) > 20:
                        break
                ctx.close()
            finally:
                browser.close()

        if token and len(token) > 20:
            log(f"✅ 自检通过: cap-token 长度 {len(token)}（耗时 {time.time()-t0:.1f}s）")
            return 0
        log("❌ 自检失败: 未取到 cap-token")
        return 1
    except Exception as e:
        log(f"❌ 自检异常: {type(e).__name__}: {str(e)[:250]}")
        return 1


def main():
    if "--selftest" in sys.argv:
        return selftest()

    accounts = load_accounts()
    if not accounts:
        notify("❌ NeoHeberg 多账号任务：无可用账号，请检查 Secrets 配置")
        return 1

    log(f"配置: 账号 {len(accounts)} 个  续期阈值 {RENEW_DAYS} 天  触发 {TRIGGER}")

    from playwright.sync_api import sync_playwright
    results = []
    cookie_cache = {}
    total = len(accounts)

    with sync_playwright() as p:
        browser, err = launch_browser(p)
        if browser is None:
            log(err)
            notify(f"❌ NeoHeberg 任务失败：{err}")
            return 1
        try:
            for idx, acct in enumerate(accounts, 1):
                results.append(process_account(browser, acct, idx, total, cookie_cache))
                if idx < total:
                    log("等待 5 秒后处理下一个账号…")
                    time.sleep(5)
        finally:
            browser.close()

    ok = sum(1 for r in results if r["ok"])
    log("=" * 46)
    log(f"全部账号处理完毕：成功 {ok} / 共 {total}")
    for idx, r in enumerate(results, 1):
        log(f"  {idx}. {r['icon']} {mask_user(r['user'])} / {mask(r['vmid'])} — {r['status']}")
    log("=" * 46)

    # 全部失败时以非零码退出，方便 Actions 标红
    return 0 if ok > 0 else 1


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
