from urllib.parse import parse_qs
import logging
from fastapi import FastAPI, Request, Response, HTTPException
from twilio.request_validator import RequestValidator
from .config import settings
from .db import Database


logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    config = settings()
    app = FastAPI(title="WhatsApp Life Agent")
    app.state.db = Database(config.supabase_url, config.supabase_service_role_key)

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await app.state.db.close()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhook/whatsapp", status_code=204)
    async def whatsapp_webhook(request: Request) -> Response:
        # Read exactly once and validate before parsing. Do not add form middleware ahead of this handler.
        raw_body = await request.body()
        signature = request.headers.get("X-Twilio-Signature", "")
        # Behind ngrok/Railway the upstream Host header can be rewritten. Twilio signs
        # the public URL it called, so reconstruct it from the configured public base.
        url = f"{config.public_base_url.rstrip('/')}{request.url.path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"
        form = {key: values[-1] for key, values in parse_qs(raw_body.decode("utf-8"), keep_blank_values=True).items()}
        if config.validate_twilio_signature:
            # Twilio signs the decoded form parameters.  We derive them from the raw bytes
            # captured above, rather than allowing a body parser to consume or mutate them.
            valid = RequestValidator(config.twilio_auth_token).validate(url, form, signature)
            if not valid:
                logger.warning("Rejected WhatsApp webhook: invalid Twilio signature")
                raise HTTPException(status_code=403, detail="Invalid Twilio signature")
        from_number, sid, body = form.get("From", ""), form.get("MessageSid", ""), form.get("Body", "")
        if from_number != config.owner_whatsapp_number:
            logger.info("Silently ignored WhatsApp sender ending in %s", from_number[-4:])
            return Response(status_code=204)
        if not sid or not body:
            return Response(status_code=204)
        rows = await app.state.db.insert("inbound_messages", {"user_id": from_number, "message_sid": sid, "from_number": from_number, "body": body}, ignore_duplicates=True)
        if rows:
            await app.state.db.rpc("enqueue_inbound_message", {"p_inbound_id": rows[0]["id"]})
            logger.info("Queued WhatsApp message %s", sid)
        else:
            logger.info("Ignored duplicate WhatsApp message %s", sid)
        return Response(status_code=204)

    return app


app = create_app()
