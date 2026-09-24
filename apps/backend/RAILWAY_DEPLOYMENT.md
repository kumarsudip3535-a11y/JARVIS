# Railway deployment — 24/7 phone ingress

This deployment keeps Twilio's webhook and the shared PostgreSQL database online
without exposing local Tally. The normal JARVIS web app and Tally integration can
continue running on Sudip's Windows computer.

## Architecture

- Railway backend: receives Twilio phone webhooks and writes call records.
- Railway PostgreSQL: shared durable database.
- Local backend/web: normal JARVIS interface and local-only integrations.
- Local backend uses Railway PostgreSQL's public connection URL so callback
  notifications created in the cloud appear in the local JARVIS interface.

Do not point Twilio at Railway until the database is migrated, the owner account
exists, and a signed test request succeeds.

## Railway service settings

Create one PostgreSQL service and one backend service from the private GitHub
repository.

Backend service:

- Root directory: `/apps/backend`
- Builder: Dockerfile
- Dockerfile: `Dockerfile`
- Health-check path: `/`
- Serverless/App Sleeping: OFF
- Restart policy: Always
- Public networking: generate a Railway HTTPS domain

The image starts with:

```text
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

## Required cloud variables

Set values in Railway's Variables screen. Never commit the values.

```text
DATABASE_URL
JWT_SECRET_KEY
AI_PRIMARY_PROVIDER
AI_SECONDARY_PROVIDER
AI_TERTIARY_PROVIDER
ANTHROPIC_API_KEY
GEMINI_API_KEY
GROQ_API_KEY
PHONE_AGENT_ENABLED=true
TWILIO_ACCOUNT_SID
TWILIO_AUTH_TOKEN
PHONE_AGENT_OWNER_EMAIL
PHONE_AGENT_PUBLIC_BASE_URL
PHONE_AGENT_VALIDATE_SIGNATURE=true
PHONE_AGENT_BUSINESS_NAME=SS Retail Services
PHONE_AGENT_TIMEZONE=Asia/Kolkata
PHONE_AGENT_LANGUAGE=en-IN
PHONE_AGENT_VOICE=Polly.Aditi
PHONE_AGENT_ALLOW_WEB_SEARCH=false
PHONE_AGENT_MAX_TURNS=15
PHONE_AGENT_REPLY_TIMEOUT_SECONDS=10
PHONE_AGENT_GREETING
PHONE_AGENT_PERSONA
```

Only provider keys that are actually used need to be present.

Use these safety settings on the cloud phone-ingress instance:

```text
AUTOMATION_ENGINE_ENABLED=false
TALLY_ENABLED=false
EMAIL_CALENDAR_ENABLED=false
DEBUG_AGENT_ENABLED=false
```

This prevents duplicate scheduled jobs and keeps computer-local integrations on
the local backend.

## Database cutover order

1. Back up the current local PostgreSQL database.
2. Create Railway PostgreSQL.
3. Restore the backup into Railway PostgreSQL.
4. Confirm Sudip's existing JARVIS user and phone records are present.
5. Set the Railway backend's `DATABASE_URL` to Railway's private database URL.
6. Set the local backend's `DATABASE_URL` to Railway's public database URL.
7. Restart both backends and run the phone regression tests locally.
8. Verify the Railway root URL returns:
   `{"status":"JARVIS backend is running"}`.
9. Set `PHONE_AGENT_PUBLIC_BASE_URL` to the Railway HTTPS domain.
10. Change Twilio's incoming-call webhook to:
    `https://<railway-domain>/api/phone/incoming`.
11. Make a live call and confirm its notification appears in local JARVIS.
12. Keep the ngrok webhook available as a temporary rollback until the Railway
    call passes.

## Rollback

If a cloud test fails, immediately restore Twilio's previous ngrok webhook. The
local phone setup remains unchanged by this deployment preparation.
