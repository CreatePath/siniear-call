import re

import httpx

from app.core.config import get_settings


class DispatchRejected(Exception):
    """The request could not have created a provider call."""


def domestic_number(number):
    if number.startswith("+82"):
        number = "0" + number[3:]
    if not re.fullmatch(r"0[1-9][0-9]{7,9}", number):
        raise DispatchRejected("INVALID_DOMESTIC_NUMBER")
    return number


class KoreaAITelProvider:
    def __init__(self, settings):
        self.settings = settings

    def create(self, execution_id, payload):
        settings = self.settings
        base = settings.public_base_url.rstrip("/") + "/api/webhooks/koreaaitel"
        response = httpx.post(
            f"https://api.koreaaitel.com/v1/accounts/{settings.koreaaitel_account_id}/calls",
            headers={"Authorization": f"Bearer {settings.koreaaitel_api_key.get_secret_value()}"},
            json={
                "To": domestic_number(payload["phone_number"]),
                "From": domestic_number(settings.koreaaitel_from_number.get_secret_value()),
                "Url": f"{base}/voice?execution_id={execution_id}",
                "StatusCallback": f"{base}/status?execution_id={execution_id}",
                "StatusCallbackEvent": "initiated ringing answered completed",
                "Timeout": 30,
                "Record": False,
            },
            timeout=15,
        )
        if response.status_code in {400, 401, 403, 404, 409, 410, 422, 429}:
            raise DispatchRejected("PROVIDER_REJECTED_REQUEST")
        response.raise_for_status()
        call_id = response.json()["callId"]
        if response.status_code != 201 or not isinstance(call_id, str) or not call_id:
            raise ValueError("Unknown dispatch outcome")
        return call_id


class FakeProvider:
    def create(self, execution_id, payload):
        return "CA" + execution_id.hex


def get_provider():
    settings = get_settings()
    if settings.call_provider == "fake":
        return FakeProvider()
    if settings.call_provider == "koreaaitel" and settings.allow_paid_calls:
        return KoreaAITelProvider(settings)
    raise RuntimeError("Call provider is disabled")
