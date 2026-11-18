from app.core.config import get_settings
from app.db.session import get_engine
from app.integrations.gemini.client import FakeModel, GeminiModel
from app.integrations.koreaaitel.client import get_provider
from app.integrations.service_server import send
from app.modules.calls.service import dispatch_one
from app.modules.processing.service import finalize_ready, process_one
from app.workers.outbox import deliver_one


def run():
    settings = get_settings()
    provider = get_provider()
    engine = get_engine()
    model = FakeModel() if settings.call_provider == "fake" else GeminiModel(settings)
    for _ in range(100):
        if not dispatch_one(engine, provider):
            break
    finalize_ready(engine)
    for _ in range(100):
        if not process_one(engine, model, settings):
            break
    for _ in range(100):
        if not deliver_one(engine, "T", send):
            break


if __name__ == "__main__":
    run()
