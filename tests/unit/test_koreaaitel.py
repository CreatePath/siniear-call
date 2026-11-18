from uuid import uuid4

import httpx
import pytest

from app.core.config import Settings
from app.integrations.koreaaitel.client import DispatchRejected, KoreaAITelProvider


def test_create_domestic_call(monkeypatch):
    settings = Settings(
        _env_file=None,
        db_password="test-only",
        koreaaitel_api_key="test-api-key",
        koreaaitel_account_id="ACsynthetic",
        koreaaitel_from_number="+827000000000",
        public_base_url="https://calls.example.test",
    )
    eid = uuid4()
    requests = []

    def post(url, *, headers, json, timeout):
        requests.append(json)
        assert url == "https://api.koreaaitel.com/v1/accounts/ACsynthetic/calls"
        assert headers["Authorization"] == "Bearer test-api-key"
        assert timeout == 15
        return httpx.Response(
            201, json={"callId": "CAsynthetic"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", post)
    provider = KoreaAITelProvider(settings)
    with pytest.raises(DispatchRejected):
        provider.create(eid, {"phone_number": "+12025550100"})
    assert provider.create(eid, {"phone_number": "+821000000001"}) == "CAsynthetic"
    base = "https://calls.example.test/api/webhooks/koreaaitel"
    assert requests == [
        {
            "To": "01000000001",
            "From": "07000000000",
            "Url": f"{base}/voice?execution_id={eid}",
            "StatusCallback": f"{base}/status?execution_id={eid}",
            "StatusCallbackEvent": "initiated ringing answered completed",
            "Timeout": 30,
            "Record": False,
        }
    ]
