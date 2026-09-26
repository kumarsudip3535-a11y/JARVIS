"""
Phase 19 "Email + Calendar" (built 2026-09-22, Sudeep's explicit choice of
what to build next right after the Multi-Model AI Brain upgrade was
live-verified). Scoped with him first via 3 clarifying questions
(`AskUserQuestion`), the same pattern as every other custom/ahead-of-schedule
feature in this project:

1. **Which email account?** -> kumar.sudip3535@gmail.com - his primary
   Gmail, the same address his JARVIS login itself uses.
2. **What can JARVIS do WITHOUT asking approval each time (v1 scope)?** ->
   "Read + draft, never auto-send" - matches the project charter's own
   wording exactly ("Email: ...draft...; sending requires confirmation by
   default"). In this v1, "confirmation" for email IS the fact that a
   created draft sits completely inert in Sudeep's own Gmail Drafts folder
   until HE presses Send there, inside Gmail's own interface - JARVIS never
   calls Gmail's send endpoint at all in this codebase, so there is no
   separate confirm-to-send flow to build. The OAuth scope below
   (`gmail.compose`) is technically capable of sending too - exactly like
   every other place in this project where a granted capability is broader
   than what the code path actually exercises (see e.g. the Tally
   ledger-auto-creation removal, or a non-read-only custom tool never
   actually being called) - enforced here in code, never invoked.
3. **Calendar in the same phase, or later?** -> Both together now, since
   they share the same one-time Google OAuth setup step and the charter
   treats "Email + Calendar" as a single phase (19).

Unlike a Gmail draft, actually creating a REAL calendar event is externally
visible - it can notify attendees and blocks real time on Sudeep's calendar -
so it is never something to fire from a single tool call. Calendar event
creation follows the exact same two-step review pattern this project already
uses for Tally billing: JARVIS's reply can carry a
`[CALENDAR_EVENT_DRAFT]{...}` block (see `ai_provider.py`'s system prompt and
`chat_routes.py`'s `_extract_calendar_draft`, deliberately modeled on the
existing `_extract_tally_draft`), the frontend renders that as a review card,
and only a real button click (`POST /api/google/calendar/create-event`)
actually calls Google Calendar's `events.insert`. Viewing the calendar and
finding open slots are genuinely read-only, so those ARE called live by the
model, the same "read-only calls live, anything else is draft/confirm-only"
rule this project already applies to custom tools (Phase 16).

This module holds the actual Google API plumbing: the OAuth 2.0
authorization-code flow (`build_auth_url` / `exchange_code_for_tokens` /
`refresh_access_token`), a helper that keeps a stored `GoogleAccount`'s
access token fresh (`get_valid_access_token`), and thin, defensive wrappers
around the small number of real Gmail/Calendar REST endpoints this feature
actually uses. Deliberately does NOT pull in `google-api-python-client` (a
large, code-generated SDK) - this project already has a working pattern for
a handful of hand-written REST calls against a well-documented API (see
`tally_client.py`'s own reasoning for staying dependency-free), and
`httpx2` (aliased to `httpx` - see `ai_provider.py`'s note on why) is
already a dependency, so no new package is needed. Every real failure mode
(an expired/revoked token, a network error, a malformed response) is handled
explicitly here and never left to bubble up as a raw exception into a chat
reply - the same standard `tally_client.py`/`custom_tools.py` already hold
themselves to.

Token encryption reuses the SAME `settings.tool_secrets_key` Fernet key
Phase 16's custom tool secrets already use (see `custom_tools.py`), rather
than asking Sudeep to generate a second key for the same standard of
protection - he already has this one configured from setting up Custom
Tools, so nothing new is required in `.env` beyond `GOOGLE_CLIENT_ID`/
`GOOGLE_CLIENT_SECRET` themselves.
"""
import base64
import datetime
import email.mime.text
import email.utils
import urllib.parse

import httpx2 as httpx

from app.config import settings

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"

# Deliberately minimal, matching exactly what v1 actually does: read/search
# Gmail and create drafts (gmail.compose - see the module docstring for why
# this is fine even though it's technically also capable of sending), and
# view + create Google Calendar events. No broader scope (e.g. full Gmail
# modify/delete, or account-wide admin scopes) is requested.
GOOGLE_OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "openid",
    "email",
]


class GoogleAuthError(Exception):
    """Raised for a real OAuth/token problem (Google rejected the code, a
    refresh failed because the token was revoked, credentials aren't
    configured, etc.) - always caught and turned into a plain, honest
    message before it ever reaches a chat reply, the same "never let one
    bad response crash the whole app" standard as tally_client.TallyError."""


def google_oauth_configured() -> bool:
    return bool(settings.google_client_id and settings.google_client_secret and settings.tool_secrets_key)


def _fernet():
    """Local Fernet helper, deliberately NOT importing custom_tools.py's own
    private _get_fernet() - this module and custom_tools.py stay decoupled,
    each with its own tiny wrapper around the same settings.tool_secrets_key,
    matching this project's general preference for small, focused modules
    over cross-module reach-ins for a two-line helper."""
    from cryptography.fernet import Fernet
    return Fernet(settings.tool_secrets_key.encode("utf-8"))


def encrypt_token(raw_value: str) -> str:
    return _fernet().encrypt(raw_value.encode("utf-8")).decode("utf-8")


def decrypt_token(encrypted_value: str) -> str:
    return _fernet().decrypt(encrypted_value.encode("utf-8")).decode("utf-8")


def build_auth_url(state: str) -> str:
    """Builds the URL Sudeep's browser is sent to for the Google consent
    screen. access_type=offline + prompt=consent guarantee a real
    refresh_token comes back even on a re-connect (Google otherwise omits it
    on a repeat authorization), which get_valid_access_token below depends
    on to keep working long after the first access token expires."""
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_oauth_redirect_uri,
        "response_type": "code",
        "scope": " ".join(GOOGLE_OAUTH_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{_AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code_for_tokens(code: str) -> dict:
    """Trades the one-time authorization code Google's redirect handed back
    for a real access_token + refresh_token. Raises GoogleAuthError with a
    plain explanation on any failure - an expired/reused code, a redirect_uri
    mismatch (the single most common real-world OAuth setup mistake), or a
    network problem all read as clear, different messages rather than a
    generic failure."""
    try:
        resp = httpx.post(
            _TOKEN_URL,
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": settings.google_oauth_redirect_uri,
            },
            timeout=20,
        )
    except httpx.HTTPError as e:
        raise GoogleAuthError(f"Couldn't reach Google's token endpoint: {e}")

    if resp.status_code != 200:
        raise GoogleAuthError(
            f"Google rejected the authorization code (HTTP {resp.status_code}): {resp.text[:500]}"
        )
    data = resp.json()
    if "refresh_token" not in data:
        raise GoogleAuthError(
            "Google didn't return a refresh token this time - this usually means the account was "
            "already connected once before without disconnecting first. Try disconnecting in "
            "JARVIS and reconnecting from scratch."
        )
    return data


def refresh_access_token(refresh_token: str) -> dict:
    """Exchanges a stored refresh_token for a fresh access_token. Raises
    GoogleAuthError on failure - most commonly because Sudeep revoked
    JARVIS's access in his Google Account settings, which is a real,
    expected case this has to explain plainly, not crash on."""
    try:
        resp = httpx.post(
            _TOKEN_URL,
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=20,
        )
    except httpx.HTTPError as e:
        raise GoogleAuthError(f"Couldn't reach Google's token endpoint: {e}")

    if resp.status_code != 200:
        raise GoogleAuthError(
            "Google refused to refresh the connection (it may have been revoked in your Google "
            f"Account settings - HTTP {resp.status_code}). Reconnect Google from JARVIS's settings."
        )
    return resp.json()


def get_valid_access_token(google_account, db) -> str:
    """Returns a real, currently-valid access token for the given
    GoogleAccount row, refreshing it first (and persisting the refresh) if
    it's expired or about to expire in the next 60 seconds. This is the one
    function every Gmail/Calendar call below goes through - callers never
    touch google_account.encrypted_access_token directly."""
    now = datetime.datetime.now(datetime.timezone.utc)
    expiry = google_account.access_token_expiry
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=datetime.timezone.utc)
    if expiry > now + datetime.timedelta(seconds=60):
        return decrypt_token(google_account.encrypted_access_token)

    refresh_token = decrypt_token(google_account.encrypted_refresh_token)
    data = refresh_access_token(refresh_token)
    new_access_token = data["access_token"]
    expires_in = data.get("expires_in", 3600)
    google_account.encrypted_access_token = encrypt_token(new_access_token)
    google_account.access_token_expiry = now + datetime.timedelta(seconds=expires_in)
    db.add(google_account)
    db.commit()
    return new_access_token


def _auth_headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def get_userinfo(access_token: str) -> dict:
    """Used right after the OAuth callback to learn which real Google
    address just got connected (so JARVIS can tell Sudeep, and so a second
    connect attempt with a different account is obviously visible)."""
    try:
        resp = httpx.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers=_auth_headers(access_token),
            timeout=15,
        )
    except httpx.HTTPError as e:
        raise GoogleAuthError(f"Couldn't reach Google to confirm the connected account: {e}")
    if resp.status_code != 200:
        raise GoogleAuthError(f"Couldn't confirm the connected Google account (HTTP {resp.status_code}).")
    return resp.json()


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

def search_emails(access_token: str, query: str, max_results: int = 10) -> list[dict]:
    """Real, live Gmail search (Gmail's own search syntax, e.g.
    "from:tallyprime is:unread newer_than:7d") - read-only, so this is
    called directly by the model with no confirmation step, the same "a
    read-only call happens live" rule Phase 16's custom tools already use.
    Returns a lightweight list (id, threadId, subject, from, date, snippet)
    - the model calls get_email below for a specific message's full body
    only when it actually needs it, so a broad search doesn't burn a full
    fetch-and-parse per result."""
    max_results = max(1, min(int(max_results or 10), 25))
    try:
        list_resp = httpx.get(
            f"{_GMAIL_BASE}/messages",
            headers=_auth_headers(access_token),
            params={"q": query, "maxResults": max_results},
            timeout=20,
        )
    except httpx.HTTPError as e:
        raise GoogleAuthError(f"Couldn't reach Gmail: {e}")
    if list_resp.status_code != 200:
        raise GoogleAuthError(f"Gmail search failed (HTTP {list_resp.status_code}): {list_resp.text[:300]}")

    ids = [m["id"] for m in list_resp.json().get("messages", [])]
    results = []
    for msg_id in ids:
        try:
            get_resp = httpx.get(
                f"{_GMAIL_BASE}/messages/{msg_id}",
                headers=_auth_headers(access_token),
                params={"format": "metadata", "metadataHeaders": ["Subject", "From", "Date"]},
                timeout=20,
            )
        except httpx.HTTPError:
            continue
        if get_resp.status_code != 200:
            continue
        data = get_resp.json()
        headers = {h["name"]: h["value"] for h in data.get("payload", {}).get("headers", [])}
        results.append({
            "id": data["id"],
            "thread_id": data.get("threadId"),
            "subject": headers.get("Subject", "(no subject)"),
            "from": headers.get("From", "(unknown sender)"),
            "date": headers.get("Date", ""),
            "snippet": data.get("snippet", ""),
        })
    return results


def _decode_body_part(payload: dict) -> str:
    """Walks a Gmail message payload for the first text/plain part (falling
    back to text/html with tags stripped only if no plain-text part exists),
    base64url-decoding it. Gmail nests multipart messages arbitrarily deeply,
    so this recurses rather than assuming a fixed shape."""
    def walk(part) -> str | None:
        mime_type = part.get("mimeType", "")
        body_data = part.get("body", {}).get("data")
        if mime_type == "text/plain" and body_data:
            return base64.urlsafe_b64decode(body_data + "==").decode("utf-8", errors="replace")
        for sub in part.get("parts", []) or []:
            found = walk(sub)
            if found is not None:
                return found
        if mime_type == "text/html" and body_data:
            import re
            html = base64.urlsafe_b64decode(body_data + "==").decode("utf-8", errors="replace")
            return re.sub("<[^>]+>", " ", html)
        return None

    return walk(payload) or ""


def get_email(access_token: str, message_id: str) -> dict:
    """Fetches one email's full content - read-only, called live. Returns
    subject/from/to/date/thread_id/body (plain text), truncated to a
    reasonable length so one long email can't blow out the reply context."""
    try:
        resp = httpx.get(
            f"{_GMAIL_BASE}/messages/{message_id}",
            headers=_auth_headers(access_token),
            params={"format": "full"},
            timeout=20,
        )
    except httpx.HTTPError as e:
        raise GoogleAuthError(f"Couldn't reach Gmail: {e}")
    if resp.status_code != 200:
        raise GoogleAuthError(f"Couldn't read that email (HTTP {resp.status_code}): {resp.text[:300]}")

    data = resp.json()
    payload = data.get("payload", {})
    headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
    body = _decode_body_part(payload)
    if len(body) > 4000:
        body = body[:4000] + "... (truncated)"
    return {
        "id": data["id"],
        "thread_id": data.get("threadId"),
        "subject": headers.get("Subject", "(no subject)"),
        "from": headers.get("From", "(unknown sender)"),
        "to": headers.get("To", ""),
        "date": headers.get("Date", ""),
        "body": body,
    }


def create_draft_reply(access_token: str, thread_id: str | None, to: str, subject: str, body: str) -> dict:
    """Creates a REAL Gmail draft in Sudeep's own Gmail account - visible in
    his Drafts folder, editable and sendable only by him, from Gmail's own
    interface. This is the one Gmail-write action v1 performs, and it is
    deliberately called live with no separate chat-side confirmation step
    (see the module docstring, scoping question 2) - a draft has zero
    effect on anyone until Sudeep himself presses Send. If thread_id is
    given, the draft is attached to that thread as a reply (Gmail then
    threads it correctly in both his and the recipient's inbox once sent);
    otherwise it's a new, standalone draft."""
    msg = email.mime.text.MIMEText(body)
    msg["to"] = to
    msg["subject"] = subject
    msg["date"] = email.utils.formatdate(localtime=True)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

    payload = {"message": {"raw": raw}}
    if thread_id:
        payload["message"]["threadId"] = thread_id

    try:
        resp = httpx.post(
            f"{_GMAIL_BASE}/drafts",
            headers=_auth_headers(access_token),
            json=payload,
            timeout=20,
        )
    except httpx.HTTPError as e:
        raise GoogleAuthError(f"Couldn't reach Gmail: {e}")
    if resp.status_code not in (200, 201):
        raise GoogleAuthError(f"Couldn't create the draft (HTTP {resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    return {"draft_id": data.get("id"), "message_id": data.get("message", {}).get("id")}


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def get_calendar_timezone(access_token: str) -> str:
    """Reads Sudeep's primary calendar's own configured timezone, rather
    than guessing or hardcoding one - used so list/create/freebusy calls
    below interpret "today"/"tomorrow"/a bare time-of-day the same way his
    actual calendar does. Falls back to Asia/Kolkata (Sudeep's own location,
    per profile) only if this call itself fails, never silently to UTC."""
    try:
        resp = httpx.get(
            f"{_CALENDAR_BASE}/calendars/primary",
            headers=_auth_headers(access_token),
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json().get("timeZone", "Asia/Kolkata")
    except httpx.HTTPError:
        pass
    return "Asia/Kolkata"


def list_calendar_events(access_token: str, time_min_iso: str, time_max_iso: str) -> list[dict]:
    """Real, live Google Calendar read - read-only, called live with no
    confirmation, same rule as search_emails above. time_min_iso/time_max_iso
    are full RFC3339 timestamps (with timezone offset) - the caller
    (chat_routes.py's tool callable) is responsible for turning whatever
    date range the model asked for into these, using the real
    current-date/time grounding ai_provider.py's system prompt already
    provides (see progress-tracker.md's Phase 18 time-grounding fix) rather
    than letting Google or this function guess."""
    try:
        resp = httpx.get(
            f"{_CALENDAR_BASE}/calendars/primary/events",
            headers=_auth_headers(access_token),
            params={
                "timeMin": time_min_iso,
                "timeMax": time_max_iso,
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": 50,
            },
            timeout=20,
        )
    except httpx.HTTPError as e:
        raise GoogleAuthError(f"Couldn't reach Google Calendar: {e}")
    if resp.status_code != 200:
        raise GoogleAuthError(f"Couldn't read your calendar (HTTP {resp.status_code}): {resp.text[:300]}")

    events = []
    for item in resp.json().get("items", []):
        start = item.get("start", {})
        end = item.get("end", {})
        events.append({
            "id": item.get("id"),
            "summary": item.get("summary", "(no title)"),
            "start": start.get("dateTime") or start.get("date"),
            "end": end.get("dateTime") or end.get("date"),
            "location": item.get("location", ""),
            "description": item.get("description", ""),
        })
    return events


def find_free_busy(access_token: str, time_min_iso: str, time_max_iso: str) -> list[dict]:
    """Real freebusy query - returns the actual busy intervals Google
    Calendar itself computed (attendee-aware, recurring-event-aware), never
    something this code tries to re-derive itself from a raw event list.
    chat_routes.py's find_open_slots tool subtracts these busy intervals
    from the requested window in real, deterministic Python (never left to
    the model to eyeball a gap on a list of events) - the same "never trust
    the AI's arithmetic" principle as Tally's tax math and the bill-
    payment-status tool."""
    try:
        resp = httpx.post(
            f"{_CALENDAR_BASE}/freeBusy",
            headers=_auth_headers(access_token),
            json={"timeMin": time_min_iso, "timeMax": time_max_iso, "items": [{"id": "primary"}]},
            timeout=20,
        )
    except httpx.HTTPError as e:
        raise GoogleAuthError(f"Couldn't reach Google Calendar: {e}")
    if resp.status_code != 200:
        raise GoogleAuthError(f"Couldn't check your calendar's free/busy (HTTP {resp.status_code}).")
    busy = resp.json().get("calendars", {}).get("primary", {}).get("busy", [])
    return busy


def create_calendar_event(
    access_token: str,
    summary: str,
    start_iso: str,
    end_iso: str,
    description: str | None = None,
    location: str | None = None,
    reminder_minutes_before: int | None = None,
    use_default_reminder: bool = True,
) -> dict:
    """Actually creates the real event - ONLY ever called from the
    /api/google/calendar/create-event endpoint (google_routes.py), which
    itself is ONLY ever reachable from a real button click on a review card
    Sudeep has seen (see this module's docstring) - never called directly
    from the chat tool-use loop. Raises GoogleAuthError on any failure
    (a malformed time, Calendar being unreachable, etc.) so the endpoint can
    report exactly why the event wasn't actually created, never silently
    claiming success.

    Fixed 2026-09-26: a browser's <input type="datetime-local"> (used by the
    calendar review card, including the newer incoming-appointment review
    flow) never carries a UTC offset by design, so start_iso/end_iso reaching
    here can be a bare "YYYY-MM-DDTHH:MM" string with no timezone info at
    all. Google Calendar's API rejects a dateTime with no offset and no
    separate timeZone field ("Missing time zone definition for start/end
    time" - found live testing the appointment-booking review card). Rather
    than trust every caller's start_iso/end_iso to already carry an offset,
    this always attaches the calendar's own real timezone explicitly, at
    this one chokepoint every caller goes through."""
    event_timezone = get_calendar_timezone(access_token)
    body = {
        "summary": summary,
        "start": {"dateTime": start_iso, "timeZone": event_timezone},
        "end": {"dateTime": end_iso, "timeZone": event_timezone},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if use_default_reminder:
        body["reminders"] = {"useDefault": True}
    else:
        body["reminders"] = {
            "useDefault": False,
            "overrides": (
                [{"method": "popup", "minutes": reminder_minutes_before}]
                if reminder_minutes_before is not None
                else []
            ),
        }

    try:
        resp = httpx.post(
            f"{_CALENDAR_BASE}/calendars/primary/events",
            headers=_auth_headers(access_token),
            json=body,
            timeout=20,
        )
    except httpx.HTTPError as e:
        raise GoogleAuthError(f"Couldn't reach Google Calendar: {e}")
    if resp.status_code not in (200, 201):
        raise GoogleAuthError(f"Couldn't create the event (HTTP {resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    return {"event_id": data.get("id"), "html_link": data.get("htmlLink")}
