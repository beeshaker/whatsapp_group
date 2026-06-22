import base64
import os
import time
from decimal import Decimal

import httpx

_SANDBOX_BASE = "https://sandbox.safaricom.co.ke"
_PROD_BASE = "https://api.safaricom.co.ke"

CONSUMER_KEY = os.getenv("MPESA_CONSUMER_KEY", "")
CONSUMER_SECRET = os.getenv("MPESA_CONSUMER_SECRET", "")
SHORTCODE = os.getenv("MPESA_SHORTCODE", "174379")  # Daraja sandbox default
PASSKEY = os.getenv("MPESA_PASSKEY", "")
MPESA_ENV = os.getenv("MPESA_ENV", "sandbox")

_token_cache: dict = {}


def _base_url() -> str:
    return _PROD_BASE if MPESA_ENV == "production" else _SANDBOX_BASE


async def get_access_token() -> str:
    now = time.time()
    if _token_cache.get("token") and now < _token_cache.get("expires_at", 0):
        return _token_cache["token"]

    credentials = base64.b64encode(f"{CONSUMER_KEY}:{CONSUMER_SECRET}".encode()).decode()
    async with httpx.AsyncClient(timeout=10.0) as http:
        r = await http.get(
            f"{_base_url()}/oauth/v1/generate?grant_type=client_credentials",
            headers={"Authorization": f"Basic {credentials}"},
        )
        r.raise_for_status()
        data = r.json()
        _token_cache["token"] = data["access_token"]
        _token_cache["expires_at"] = now + int(data["expires_in"]) - 60
        return _token_cache["token"]


def _stk_password(timestamp: str) -> str:
    return base64.b64encode(f"{SHORTCODE}{PASSKEY}{timestamp}".encode()).decode()


async def initiate_stk_push(phone: str, amount: Decimal, account_ref: str, callback_url: str) -> dict:
    """Trigger M-Pesa STK Push. Returns Daraja response dict."""
    token = await get_access_token()
    timestamp = time.strftime("%Y%m%d%H%M%S")
    async with httpx.AsyncClient(timeout=15.0) as http:
        r = await http.post(
            f"{_base_url()}/mpesa/stkpush/v1/processrequest",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "BusinessShortCode": SHORTCODE,
                "Password": _stk_password(timestamp),
                "Timestamp": timestamp,
                "TransactionType": "CustomerPayBillOnline",
                "Amount": int(amount),
                "PartyA": phone,
                "PartyB": SHORTCODE,
                "PhoneNumber": phone,
                "CallBackURL": callback_url,
                "AccountReference": account_ref,
                "TransactionDesc": "Subscription payment",
            },
        )
        r.raise_for_status()
        return r.json()
