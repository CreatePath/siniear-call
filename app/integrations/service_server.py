import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.core.config import get_settings


class ServiceClient:
    def request(self, method, path, payload=None, key=None):
        settings = get_settings()
        token = settings.internal_outbound_token.get_secret_value()
        if not token:
            raise RuntimeError("Service token is not configured")
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        request = Request(
            settings.peer_base_url.rstrip("/") + "/api" + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
            headers=headers,
        )
        try:
            with urlopen(request, timeout=15) as response:
                return response.status, json.load(response) if response.status != 204 else None
        except HTTPError as exc:
            return exc.code, None


def send(operation, resource_id, key, payload):
    if operation == "call.interests":
        from sqlalchemy.orm import Session

        from app.core.errors import DomainError
        from app.db.session import get_engine
        from app.modules.calls.repository import require_eligible, senior_for_call
        from app.workers.outbox import DeliveryCanceled

        try:
            with Session(get_engine()) as session:
                require_eligible(session, senior_for_call(session, resource_id))
        except DomainError:
            raise DeliveryCanceled from None
    paths = {
        "call.event": f"/internal/calls/{resource_id}/events",
        "delivery.event": f"/internal/alert-deliveries/{resource_id}/events",
        "call.interests": f"/internal/calls/{resource_id}/interests",
    }
    status, _ = ServiceClient().request("POST", paths[operation], payload, key)
    if operation == "call.interests" and status in {403, 404}:
        from app.workers.outbox import DeliveryCanceled

        raise DeliveryCanceled
    expected = 200 if operation == "call.interests" else 204
    if status != expected:
        raise RuntimeError("Service acknowledgement failed")
