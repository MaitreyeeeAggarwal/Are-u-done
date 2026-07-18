# WhatsApp Life Agent

A durable WhatsApp task, goal, review, and exam-planning agent built with FastAPI, Supabase, Twilio, and NVIDIA NIM.

## Run locally

1. Install Python 3.11+ and create a virtual environment.
2. Copy `.env.example` to `.env`, then fill in Twilio, Supabase, NVIDIA, and your WhatsApp owner number.
3. Run the SQL in `supabase/migrations/001_initial.sql` in the Supabase SQL editor.
4. Install the project: `pip install -e ".[dev]"`.
5. Start the web service with `uvicorn app.main:app --reload` and the worker separately with `python -m app.worker`.
6. Expose the web service with `ngrok http 8000`; configure Twilio Sandbox to post to `/webhook/whatsapp`.

For local smoke tests only, set `VALIDATE_TWILIO_SIGNATURE=false`. Always enable it for a real Twilio webhook.

## Railway

Create two Railway services from this repository. The web service uses the `web` Procfile command; the worker service uses `worker`. Set the same environment variables on both services. The worker polls durable Supabase records every five minutes, so no reminder is stored only in process memory.

## Important operational behavior

- The webhook captures the raw request body before decoding form fields for Twilio validation.
- Incoming Twilio SIDs are idempotent and per-user queue leases expire after crashes.
- Numbered completions use the saved list snapshot shown to the user, not the current ranking.
- Conversation state expires after 24 hours.
- Outbound sends retry with backoff. Failed sends are retained as `failed` records for daily-review reporting.
