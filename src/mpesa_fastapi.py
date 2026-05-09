import base64
import os
from datetime import datetime
from typing import Any

import httpx


SANDBOX_BASE_URL = "https://sandbox.safaricom.co.ke"
LIVE_BASE_URL = "https://api.safaricom.co.ke"


class MpesaConfigError(ValueError):
    pass


def normalize_phone_number(phone_number: str) -> str:
    digits = "".join(ch for ch in str(phone_number or "") if ch.isdigit())
    if digits.startswith("0") and len(digits) == 10:
        return f"254{digits[1:]}"
    if digits.startswith("254") and len(digits) == 12:
        return digits
    if digits.startswith("7") and len(digits) == 9:
        return f"254{digits}"
    raise MpesaConfigError("Phone number must be Kenyan format like 07XXXXXXXX or 2547XXXXXXXX.")


class MpesaService:
    def __init__(
        self,
        *,
        env: str,
        consumer_key: str,
        consumer_secret: str,
        short_code: str,
        passkey: str,
        callback_url: str,
    ) -> None:
        self.env = (env or "sandbox").strip().lower()
        self.consumer_key = consumer_key.strip()
        self.consumer_secret = consumer_secret.strip()
        self.short_code = short_code.strip()
        self.passkey = passkey.strip()
        self.callback_url = callback_url.strip()

        if not all(
            [
                self.consumer_key,
                self.consumer_secret,
                self.short_code,
                self.passkey,
                self.callback_url,
            ]
        ):
            raise MpesaConfigError(
                "M-Pesa is not fully configured. Set MPESA_CONSUMER_KEY, MPESA_CONSUMER_SECRET, "
                "MPESA_SHORTCODE, MPESA_PASSKEY, and MPESA_CALLBACK_URL."
            )

    @classmethod
    def from_env(cls, callback_url: str | None = None) -> "MpesaService":
        return cls(
            env=os.getenv("MPESA_ENV", "sandbox"),
            consumer_key=os.getenv("MPESA_CONSUMER_KEY", ""),
            consumer_secret=os.getenv("MPESA_CONSUMER_SECRET", ""),
            short_code=os.getenv("MPESA_SHORTCODE", ""),
            passkey=os.getenv("MPESA_PASSKEY", ""),
            callback_url=callback_url or os.getenv("MPESA_CALLBACK_URL", ""),
        )

    @property
    def base_url(self) -> str:
        return LIVE_BASE_URL if self.env == "production" else SANDBOX_BASE_URL

    async def get_access_token(self, client: httpx.AsyncClient) -> str:
        response = await client.get(
            f"{self.base_url}/oauth/v1/generate",
            params={"grant_type": "client_credentials"},
            auth=(self.consumer_key, self.consumer_secret),
        )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise MpesaConfigError("Safaricom token response did not include an access token.")
        return token

    def generate_timestamp(self) -> str:
        return datetime.now().strftime("%Y%m%d%H%M%S")

    def generate_password(self, timestamp: str) -> str:
        raw = f"{self.short_code}{self.passkey}{timestamp}"
        return base64.b64encode(raw.encode("utf-8")).decode("utf-8")

    async def stk_push(
        self,
        client: httpx.AsyncClient,
        *,
        phone_number: str,
        amount: float,
        reference: str,
        description: str,
    ) -> dict[str, Any]:
        normalized_phone = normalize_phone_number(phone_number)
        token = await self.get_access_token(client)
        timestamp = self.generate_timestamp()
        payload = {
            "BusinessShortCode": self.short_code,
            "Password": self.generate_password(timestamp),
            "Timestamp": timestamp,
            "TransactionType": "CustomerPayBillOnline",
            "Amount": int(round(float(amount))),
            "PartyA": normalized_phone,
            "PartyB": self.short_code,
            "PhoneNumber": normalized_phone,
            "CallBackURL": self.callback_url,
            "AccountReference": reference[:12],
            "TransactionDesc": description[:13],
        }
        response = await client.post(
            f"{self.base_url}/mpesa/stkpush/v1/processrequest",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        data = response.json()
        return {
            "request": payload,
            "response": data,
            "phone_number": normalized_phone,
            "timestamp": timestamp,
        }

    async def stk_push_query(
        self,
        client: httpx.AsyncClient,
        *,
        checkout_request_id: str,
    ) -> dict[str, Any]:
        token = await self.get_access_token(client)
        timestamp = self.generate_timestamp()
        payload = {
            "BusinessShortCode": self.short_code,
            "Password": self.generate_password(timestamp),
            "Timestamp": timestamp,
            "CheckoutRequestID": checkout_request_id,
        }
        response = await client.post(
            f"{self.base_url}/mpesa/stkpushquery/v1/query",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        return response.json()


def parse_stk_callback(payload: dict[str, Any]) -> dict[str, Any]:
    callback = ((payload or {}).get("Body") or {}).get("stkCallback") or {}
    metadata = callback.get("CallbackMetadata") or {}
    items = metadata.get("Item") or []

    parsed: dict[str, Any] = {
        "merchant_request_id": callback.get("MerchantRequestID"),
        "checkout_request_id": callback.get("CheckoutRequestID"),
        "result_code": str(callback.get("ResultCode", "")),
        "result_desc": callback.get("ResultDesc", ""),
        "receipt_number": None,
        "phone_number": None,
        "amount": None,
        "transaction_date": None,
    }

    for item in items:
        name = item.get("Name")
        value = item.get("Value")
        if name == "MpesaReceiptNumber":
            parsed["receipt_number"] = value
        elif name == "PhoneNumber":
            parsed["phone_number"] = str(value) if value is not None else None
        elif name == "Amount":
            parsed["amount"] = float(value)
        elif name == "TransactionDate" and value:
            parsed["transaction_date"] = datetime.strptime(str(value), "%Y%m%d%H%M%S").isoformat()

    return parsed
