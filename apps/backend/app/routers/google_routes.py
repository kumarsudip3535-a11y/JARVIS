"""Phase 19 "Email + Calendar" (added 2026-09-22, scoped with Sudeep via 3
AskUserQuestion questions - see progress-tracker.md and google_client.py's
own docstring for the full design). This router holds everything that needs
a real HTTP endpoint rather than a chat tool call: the OAuth connect/
disconnect flow (a real browser round-trip through Google's consent screen,
which a chat tool call can't do), a status endpoint the settings page and
chat health check can both read, and the one real write endpoint -
POST /calendar/create-event - that a confirmed [CALENDAR_EVENT_DRAFT] review
card's "Create Event" button calls (see chat_routes.py's _extract_calendar_
draft and schemas.py's CalendarEventDraft), mirroring tally_routes.py's own
create_bill endpoint pattern exactly.

OAuth state handling: Google's own redirect back to /oauth/callback is a
plain browser navigation with no Authorization header, so there's no way to
identify which JARVIS user this is for from get_current_user the normal way.
_oauth_states is a small, short-lived, in-memory map from a random state
token (handed to Google on the way out via oauth/start, authenticated
normally) back to the real user_id, checked and consumed on the way back in
via oauth/callback - the same one-time-use CSRF-state pattern real OAuth
integrations use, kept intentionally simple (no new DB table) since this
backend is a single long-running process and the whole round-trip normally
takes under a minute in the same browser session."""
import datetime
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User, GoogleAccount
from app.schemas import CalendarEventDraft, CalendarEventResult, GoogleAuthUrlOut, GoogleStatusOut
from app.auth import get_current_user
from app import google_client

router = APIRouter(prefix="/api/google", tags=["google"])

# state -> {"user_id": int, "expires_at": datetime} - see module docstring.
# Cleared as it's consumed (or found expired) below; never persisted, so a
# backend restart mid-flow just means Sudeep has to click "Connect Google
# Account" again rather than anything silently breaking.
_oauth_states: dict = {}

_STATE_TTL_SECONDS = 600  # 10 minutes - plenty for a real consent-screen click-through


def _new_state(user_id: int) -> str:
    # Opportunistic cleanup of any stale/expired entries, so a long-running
    # backend never accumulates abandoned OAuth attempts forever - cheap
    # since this only ever holds a handful of entries at once in practice.
    now = datetime.datetime.now(datetime.timezone.utc)
    expired = [s for s, v in _oauth_states.items() if v["expires_at"] < now]
    for s in expired:
        _oauth_states.pop(s, None)

    state = secrets.token_urlsafe(24)
    _oauth_states[state] = {
        "user_id": user_id,
        "expires_at": now + datetime.timedelta(seconds=_STATE_TTL_SECONDS),
    }
    return state


def _consume_state(state: str) -> "int | None":
    entry = _oauth_states.pop(state, None)
    if entry is None:
        return None
    if entry["expires_at"] < datetime.datetime.now(datetime.timezone.utc):
        return None
    return entry["user_id"]


def _require_enabled():
    if not settings.email_calendar_enabled:
        raise HTTPException(status_code=403, detail="Email + Calendar integration is disabled")
    if not google_client.google_oauth_configured():
        missing = []
        if not settings.google_client_id:
            missing.append("GOOGLE_CLIENT_ID")
        if not settings.google_client_secret:
            missing.append("GOOGLE_CLIENT_SECRET")
        if not settings.tool_secrets_key:
            missing.append("TOOL_SECRETS_KEY")
        raise HTTPException(
            status_code=400,
            detail=(
                "Google isn't configured yet - "
                + "/".join(missing)
                + " need to be set in the backend's .env first (see the Google "
                "Cloud Console setup steps). TOOL_SECRETS_KEY is also required even "
                "though it's not a Google-specific setting - it's what encrypts your "
                "saved Google tokens."
            ),
        )


def _get_account(db: Session, user_id: int) -> "GoogleAccount | None":
    return db.query(GoogleAccount).filter(GoogleAccount.user_id == user_id).first()


@router.get("/status", response_model=GoogleStatusOut)
def google_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not settings.email_calendar_enabled:
        return {"connected": False, "google_email": None, "connected_at": None, "scopes": []}
    account = _get_account(db, current_user.id)
    if not account:
        return {"connected": False, "google_email": None, "connected_at": None, "scopes": []}
    import json as _json
    try:
        scopes = _json.loads(account.scopes or "[]")
    except (ValueError, TypeError):
        scopes = []
    return {
        "connected": True,
        "google_email": account.google_email,
        "connected_at": account.connected_at,
        "scopes": scopes,
    }


@router.get("/oauth/start", response_model=GoogleAuthUrlOut)
def oauth_start(current_user: User = Depends(get_current_user)):
    """Called by the Google-connect settings page's "Connect Google Account"
    button (a normal authenticated fetch) - returns the real Google consent
    screen URL for the frontend to navigate the browser to. access_type=
    offline + prompt=consent (see google_client.build_auth_url) guarantee a
    refresh_token comes back even if Sudeep is reconnecting after a prior
    disconnect."""
    _require_enabled()
    state = _new_state(current_user.id)
    return {"auth_url": google_client.build_auth_url(state)}


@router.get("/oauth/callback")
def oauth_callback(request: Request, db: Session = Depends(get_db)):
    """Google's own redirect target after Sudeep approves (or denies) the
    consent screen - a plain browser navigation, never called directly by
    the frontend. Always ends by redirecting the browser back to the
    settings page (http://localhost:3000, matching main.py's CORS origin -
    this whole app is local-only) with a query param the page reads to show
    success or a plain explanation, rather than showing Sudeep a bare JSON
    error on a backend URL he never otherwise sees."""
    settings_url = "http://localhost:3000/settings/google"
    params = dict(request.query_params)

    if params.get("error"):
        return RedirectResponse(f"{settings_url}?error={params['error']}")

    state = params.get("state", "")
    code = params.get("code", "")
    user_id = _consume_state(state)
    if user_id is None:
        return RedirectResponse(f"{settings_url}?error=expired_or_invalid_state")
    if not code:
        return RedirectResponse(f"{settings_url}?error=no_code_returned")

    try:
        token_data = google_client.exchange_code_for_tokens(code)
        access_token = token_data["access_token"]
        userinfo = google_client.get_userinfo(access_token)
    except google_client.GoogleAuthError as e:
        return RedirectResponse(f"{settings_url}?error={str(e)[:200]}")
    except Exception:
        return RedirectResponse(f"{settings_url}?error=unexpected_error")

    now = datetime.datetime.now(datetime.timezone.utc)
    expires_in = token_data.get("expires_in", 3600)
    google_email = userinfo.get("email", "")

    account = _get_account(db, user_id)
    if account is None:
        account = GoogleAccount(user_id=user_id)
        db.add(account)

    import json as _json
    account.google_email = google_email
    account.encrypted_access_token = google_client.encrypt_token(access_token)
    account.encrypted_refresh_token = google_client.encrypt_token(token_data["refresh_token"])
    account.access_token_expiry = now + datetime.timedelta(seconds=expires_in)
    account.scopes = _json.dumps(google_client.GOOGLE_OAUTH_SCOPES)
    db.commit()

    return RedirectResponse(f"{settings_url}?connected=1")


@router.post("/disconnect")
def disconnect(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = _get_account(db, current_user.id)
    if account:
        db.delete(account)
        db.commit()
    return {"status": "disconnected"}


@router.post("/calendar/create-event", response_model=CalendarEventResult)
def create_calendar_event(
    draft: CalendarEventDraft,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The confirm step of the [CALENDAR_EVENT_DRAFT] review-card flow (see
    ai_provider.py's system prompt and chat_routes.py's _extract_calendar_
    draft) - only ever called from a real button click, mirroring
    tally_routes.create_bill exactly. A real Google Calendar event is
    externally visible (can notify attendees, blocks real time), so this is
    the ONLY code path in this feature that ever actually creates one."""
    _require_enabled()
    account = _get_account(db, current_user.id)
    if not account:
        raise HTTPException(
            status_code=400,
            detail="Google Calendar isn't connected yet - connect it from Settings first.",
        )
    if not draft.summary.strip():
        raise HTTPException(status_code=400, detail="The event needs a title")
    if not draft.start_iso or not draft.end_iso:
        raise HTTPException(status_code=400, detail="The event needs a start and end time")

    try:
        access_token = google_client.get_valid_access_token(account, db)
    except google_client.GoogleAuthError as e:
        raise HTTPException(status_code=502, detail=str(e))

    try:
        result = google_client.create_calendar_event(
            access_token,
            draft.summary,
            draft.start_iso,
            draft.end_iso,
            description=draft.description,
            location=draft.location,
            reminder_minutes_before=draft.reminder_minutes_before,
            use_default_reminder=draft.use_default_reminder,
        )
    except google_client.GoogleAuthError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {
        "success": True,
        "message": f"Created \"{draft.summary}\" on your calendar.",
        "event_id": result.get("event_id"),
        "html_link": result.get("html_link"),
    }
