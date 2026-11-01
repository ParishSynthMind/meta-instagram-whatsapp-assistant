"""FastAPI webhook receiver.

  GET  /webhooks/instagram  and  /webhooks/whatsapp   Meta's verification handshake
  POST /webhooks/instagram  and  /webhooks/whatsapp   signed event deliveries
  GET  /health

Each POST is checked against X-Hub-Signature-256 before anything is parsed,
answered with 200 straight away, and processed in a background task, so a
slow model call never makes Meta think the delivery failed.
"""
import json
import logging

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import PlainTextResponse

from .config import Settings
from .handlers import Assistant
from .signature import is_valid_signature

MAX_BODY = 1_000_000
EXPECTED_OBJECT = {"instagram": "instagram", "whatsapp": "whatsapp_business_account"}

log = logging.getLogger("meta_assistant")


def build_assistant(settings: Settings) -> Assistant:
    from .llm import AnthropicReplier
    from .meta_api import MetaClient
    from .store import Store

    store = Store(settings.db_path, settings.reply_log_path)
    return Assistant(settings, store, AnthropicReplier(settings.model), MetaClient(settings))


def create_app(settings: Settings | None = None, assistant: Assistant | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    assistant = assistant or build_assistant(settings)
    app = FastAPI(title="Meta Instagram + WhatsApp assistant", docs_url=None, redoc_url=None)
    app.state.assistant = assistant

    def secret_for(product: str) -> str:
        return settings.instagram_secret if product == "instagram" else settings.meta_app_secret

    def enabled(product: str) -> bool:
        return settings.enable_instagram if product == "instagram" else settings.enable_whatsapp

    @app.get("/webhooks/{product}")
    async def verify(product: str, request: Request):
        q = request.query_params
        if product not in EXPECTED_OBJECT or not settings.verify_token:
            return Response(status_code=404)
        if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == settings.verify_token:
            return PlainTextResponse(q.get("hub.challenge", ""))
        return Response(status_code=403)

    @app.post("/webhooks/{product}")
    async def receive(product: str, request: Request, background: BackgroundTasks):
        if product not in EXPECTED_OBJECT or not enabled(product):
            return Response(status_code=404)
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return Response(status_code=413)
        if not is_valid_signature(secret_for(product), raw, request.headers.get("X-Hub-Signature-256")):
            log.warning("rejected %s webhook: bad or missing signature", product)
            return Response(status_code=403, content="invalid signature")
        try:
            payload = json.loads(raw)
        except ValueError:
            return Response(status_code=400, content="invalid json")
        if payload.get("object") != EXPECTED_OBJECT[product]:
            return Response(status_code=200, content="ignored")  # 200 so Meta doesn't retry
        background.add_task(assistant.process_payload, product, payload)
        return PlainTextResponse("EVENT_RECEIVED")

    @app.get("/health")
    async def health():
        return PlainTextResponse("ok")

    return app


def main():
    import os

    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    uvicorn.run(create_app(), host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
