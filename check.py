#!/usr/bin/env python3
"""IRE backup uptime monitor (GitHub Actions). Stdlib only.

  python3 check.py               # run all checks, update state, alert on state changes
  python3 check.py --test-alert  # also send one test Telegram message

Telegram credentials come ONLY from env (Actions secrets):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
State file: state/state.json (persisted between runs via actions/cache).
Always exits 0: this is a notifier, not a CI gate.
"""
import json, os, socket, ssl, sys, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Madrid")
except Exception:  # pragma: no cover
    TZ = timezone.utc

UA = "IRE-Backup-Monitor/1.0 (GitHub Actions; IRE Digital Media)"
TIMEOUT = 20
SLOW_MS = 3000
FAILS_FOR_DOWN = 2
TLS_ALERT_DAYS = 14
STATE_FILE = os.path.join("state", "state.json")

PAGES = [
    {"id": "ireclaw_home",    "url": "https://ireclaw.com",         "text": "Autonomous AI Agents for Business"},
    {"id": "ireclaw_pricing", "url": "https://ireclaw.com/pricing", "text": "Choose the operator level"},
    {"id": "ireclaw_login",   "url": "https://ireclaw.com/login",   "text": "Log in to your account"},
    {"id": "iredigital_home", "url": "https://iredigitalmedia.com", "text": "IRE Digital Media"},
    {"id": "irevideo_home",   "url": "https://irevideo.com",        "text": "AI Editor for TikTok, Reels, and YouTube Shorts"},
]
ENDPOINTS = [
    {"id": "ireclaw_api_health",          "kind": "health",   "method": "GET",  "url": "https://ireclaw.com/api/health",                  "expect": [200]},
    {"id": "irevideo_api_health",         "kind": "health",   "method": "GET",  "url": "https://irevideo.com/api/health",                 "expect": [200]},
    {"id": "ireclaw_stripe_webhook",      "kind": "webhook",  "method": "POST", "url": "https://ireclaw.com/api/stripe/webhook",          "expect": [400, 401, 403]},
    {"id": "ireclaw_webhooks_stripe",     "kind": "webhook",  "method": "POST", "url": "https://ireclaw.com/api/webhooks/stripe",         "expect": [400, 401, 403]},
    {"id": "iredigital_stripe_webhook",   "kind": "webhook",  "method": "POST", "url": "https://iredigitalmedia.com/api/stripe/webhook",  "expect": [400, 401, 403]},
    {"id": "iredigital_webhooks_stripe",  "kind": "webhook",  "method": "POST", "url": "https://iredigitalmedia.com/api/webhooks/stripe", "expect": [400, 401, 403]},
    {"id": "irevideo_webhooks_stripe",    "kind": "webhook",  "method": "POST", "url": "https://irevideo.com/api/webhooks/stripe",        "expect": [400, 401, 403]},
    {"id": "ireclaw_checkout_session",    "kind": "checkout", "method": "GET",  "url": "https://ireclaw.com/api/checkout/session",        "expect": [405]},
    {"id": "iredigital_checkout_session", "kind": "checkout", "method": "GET",  "url": "https://iredigitalmedia.com/api/checkout/session", "expect": [405]},
    {"id": "irevideo_checkout_session",   "kind": "checkout", "method": "GET",  "url": "https://irevideo.com/api/checkout/session",       "expect": [405]},
]
TLS_HOSTS = ["ireclaw.com", "iredigitalmedia.com", "irevideo.com"]
PENDING = [{"id": "agent_ireclaw", "host": "agent.ireclaw.com", "url": "https://agent.ireclaw.com"}]


def now_s():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M %Z")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPEN_FOLLOW = urllib.request.build_opener()
OPEN_NOFOLLOW = urllib.request.build_opener(NoRedirect)


def http(method, url, follow=True, body=None):
    headers = {"User-Agent": UA, "Accept": "*/*"}
    data = None
    if body is not None:
        data = body.encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    opener = OPEN_FOLLOW if follow else OPEN_NOFOLLOW
    t0 = time.time()
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            text = r.read(3_000_000).decode("utf-8", "replace")
            return {"code": r.status, "ms": round((time.time() - t0) * 1000), "body": text, "err": ""}
    except urllib.error.HTTPError as e:
        try:
            text = e.read(200_000).decode("utf-8", "replace")
        except Exception:
            text = ""
        return {"code": e.code, "ms": round((time.time() - t0) * 1000), "body": text, "err": ""}
    except Exception as e:
        return {"code": 0, "ms": None, "body": "", "err": "%s: %s" % (type(e).__name__, str(e)[:100])}


def http_retry(method, url, **kw):
    r = http(method, url, **kw)
    if r["code"] == 0 or r["code"] >= 500:
        time.sleep(5)
        r = http(method, url, **kw)
        r["retried"] = True
    return r


def check_page(p):
    r = http_retry("GET", p["url"], follow=True)
    res = {"id": p["id"], "kind": "page", "url": p["url"], "code": r["code"], "ms": r["ms"]}
    if r["code"] == 0 or r["code"] >= 500:
        res["status"], res["detail"] = "fail", r["err"] or "HTTP %d" % r["code"]
    elif r["code"] != 200:
        res["status"], res["detail"] = "fail", "HTTP %d (esperado 200)" % r["code"]
    elif p.get("text") and p["text"] not in r["body"]:
        res["status"], res["detail"] = "fail", "texto clave no encontrado: %r" % p["text"]
    else:
        res["status"] = "ok"
        res["detail"] = "texto OK" if p.get("text") else ""
    if res["status"] == "ok" and r["ms"] and r["ms"] > SLOW_MS:
        res["detail"] += " | lento (> %d ms)" % SLOW_MS
    return res


def check_endpoint(e):
    body = "{}" if e["method"] == "POST" else None
    r = http_retry(e["method"], e["url"], follow=False, body=body)
    res = {"id": e["id"], "kind": e["kind"], "url": e["url"], "method": e["method"], "code": r["code"], "ms": r["ms"]}
    if r["code"] == 0 or r["code"] >= 500:
        res["status"], res["detail"] = "fail", r["err"] or "HTTP %d" % r["code"]
    elif r["code"] in e["expect"]:
        res["status"], res["detail"] = "ok", "esperado %s" % e["expect"]
    elif e["kind"] == "health":
        res["status"], res["detail"] = "fail", "HTTP %d (esperado 200)" % r["code"]
    else:
        # same as the box monitor: unexpected but not 5xx -> warning, logged, no alert
        res["status"] = "warn"
        res["detail"] = "HTTP %d inesperado (esperado %s)%s" % (
            r["code"], e["expect"], " - acepta POST sin firma" if e["kind"] == "webhook" and 200 <= r["code"] < 300 else "")
    return res


def check_tls(host):
    res = {"id": "tls_" + host, "kind": "tls", "host": host, "code": None, "ms": None}
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=10) as s:
            with ctx.wrap_socket(s, server_hostname=host) as ss:
                cert = ss.getpeercert()
        exp = ssl.cert_time_to_seconds(cert["notAfter"])
        days = round((exp - time.time()) / 86400, 1)
        res["days"] = days
        res["not_after"] = datetime.fromtimestamp(exp, TZ).strftime("%Y-%m-%d")
        if days < TLS_ALERT_DAYS:
            res["status"], res["detail"] = "tls_expiring", "caduca en %.1f días (%s)" % (days, res["not_after"])
        else:
            res["status"], res["detail"] = "ok", "%.0f días (expira %s)" % (days, res["not_after"])
    except Exception as e:
        res["days"] = None
        res["status"], res["detail"] = "fail", "%s: %s" % (type(e).__name__, str(e)[:100])
    return res


def resolves(host):
    try:
        return bool(socket.getaddrinfo(host, 443))
    except socket.gaierror:
        return False
    except Exception:
        return False


def check_pending(pp):
    if not resolves(pp["host"]):
        return [{"id": pp["id"], "kind": "pending", "url": pp["url"], "code": None, "ms": None,
                 "status": "pending", "detail": "pending DNS (sin alerta)"}]
    page = check_page({"id": pp["id"], "url": pp["url"], "text": ""})
    page["detail"] = (page["detail"] + " | " if page["detail"] else "") + "DNS ya resuelve"
    return [page, check_tls(pp["host"])]


def run_checks():
    with ThreadPoolExecutor(max_workers=8) as ex:
        fp = [ex.submit(check_page, p) for p in PAGES]
        fe = [ex.submit(check_endpoint, e) for e in ENDPOINTS]
        ft = [ex.submit(check_tls, h) for h in TLS_HOSTS]
        fpp = [ex.submit(check_pending, pp) for pp in PENDING]
        results = [f.result() for f in fp + fe + ft]
        for f in fpp:
            results += f.result()
    return results


# ------------------------------------------------------------------ state / alerts
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def evaluate(results, state):
    """Alert only on state changes. down/up need 2 consecutive failures; TLS < 14d alerts once."""
    checks = state.setdefault("checks", {})
    lines = []
    ts = now_s()
    for r in results:
        st = checks.setdefault(r["id"], {"state": None, "fails": 0, "since": ts})
        old = st.get("state")
        if r["kind"] == "tls":
            if r["status"] == "tls_expiring":
                new = "tls_expiring"
                st["fails"] = 0
            elif r["status"] == "fail":
                st["fails"] = st.get("fails", 0) + 1
                new = "down" if st["fails"] >= FAILS_FOR_DOWN else (old or "ok")
            else:
                st["fails"], new = 0, "ok"
        elif r["status"] == "pending":
            st["fails"], new = 0, "pending"
        elif r["status"] == "fail":
            st["fails"] = st.get("fails", 0) + 1
            new = "down" if st["fails"] >= FAILS_FOR_DOWN else (old or "ok")
        else:
            st["fails"] = 0
            new = r["status"]  # ok | warn
        r["effective"] = new
        st.update({"last": ts, "code": r.get("code"), "ms": r.get("ms"), "detail": r.get("detail", "")})
        if new == old:
            continue
        st["prev_state"], st["state"], st["since"] = old, new, ts
        name = r["id"]
        where = r.get("url") or r.get("host") or ""
        if new == "down":
            lines.append("🔴 CAÍDO: %s %s — %s (%d fallos seguidos)" % (name, where, r.get("detail"), st["fails"]))
        elif new == "tls_expiring":
            lines.append("🔐 TLS %s caduca en %.1f días" % (r.get("host"), r.get("days") or 0))
        elif old == "down":
            lines.append("🟢 RECUPERADO: %s — HTTP %s en %s ms" % (name, r.get("code"), r.get("ms")))
        elif old == "tls_expiring" and new == "ok":
            lines.append("🔐 TLS %s renovado: %s días" % (r.get("host"), r.get("days")))
        elif old == "pending" and new != "pending":
            lines.append("ℹ️ %s: DNS ya resuelve, empiezo a monitorizar" % name)
    return lines


# ------------------------------------------------------------------ telegram
def telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("telegram not configured")
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % token, data=data,
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            ok = json.loads(r.read().decode()).get("ok") is True
            print("telegram sent ok=%s" % ok)
            return ok
    except urllib.error.HTTPError as e:
        print("telegram error HTTP %d" % e.code)  # never print the URL (contains the token)
    except Exception as e:
        print("telegram error %s" % type(e).__name__)
    return False


# ------------------------------------------------------------------ main
def print_table(results):
    rows = []
    print("%-28s %-8s %-13s %5s %7s  %s" % ("check", "kind", "state", "code", "ms", "detail"))
    for r in results:
        code = r.get("code") if r.get("code") is not None else "-"
        ms = r.get("ms") if r.get("ms") is not None else "-"
        print("%-28s %-8s %-13s %5s %7s  %s" % (r["id"], r["kind"], r.get("effective", r["status"]), code, ms, r.get("detail", "")))
        rows.append("| %s | %s | %s | %s | %s | %s |" % (r["id"], r["kind"], r.get("effective", r["status"]), code, ms, r.get("detail", "").replace("|", "/")))
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a") as f:
            f.write("## IRE backup monitor — %s\n\n| check | kind | state | code | ms | detail |\n|---|---|---|---|---|---|\n" % now_s())
            f.write("\n".join(rows) + "\n")


def main():
    test_alert = "--test-alert" in sys.argv
    if test_alert:
        telegram("Backup monitor IRE (GitHub Actions) activo ✅")
    t0 = time.time()
    results = run_checks()
    state = load_state()
    lines = evaluate(results, state)
    print_table(results)
    counts = {}
    for r in results:
        counts[r["effective"]] = counts.get(r["effective"], 0) + 1
    print("run %ss counts=%s alerts=%d prev_run=%s" % (round(time.time() - t0, 1), counts, len(lines), state.get("last_run")))
    if lines:
        print("ALERT:\n" + "\n".join(lines))
        telegram("⚠️ Backup monitor IRE (GH Actions) — %s\n%s" % (now_s(), "\n".join(lines)))
    state["last_run"] = now_s()
    state["run_count"] = state.get("run_count", 0) + 1
    save_state(state)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # never fail the job
        print("check.py error: %s: %s" % (type(e).__name__, e))
        sys.exit(0)
