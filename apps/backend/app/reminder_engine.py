"""Reminder feature (added 2026-09-26, per Sudeep's explicit request made
during the Phase 21/22 auto-booking round: "I also want JARVIS to remind
me of my appointments and my bookings before the appointment", and his own
choice of delivery mechanism when asked - a real phone call or SMS via
Twilio, over a chat-only reminder or waiting for Phase 24 "Proactive
intelligence". SMS was picked over an actual reminder call for v1: it is
far simpler to build reliably (a one-shot Twilio Messages API call, no
TwiML/Gather loop, nothing to answer), doesn't interrupt Sudeep mid-task
the way a ringing phone does, and still reaches him even if JARVIS's own
backend is the only thing that knows about it. A voice-call reminder could
be added later re-using Phase 20/21's existing outbound-call machinery if
Sudeep would rather have that instead.

Google Calendar itself (via google_client.list_calendar_events) is the
single source of truth for what is actually on Sudeep's calendar - this
module never keeps its own copy of event details. It only tracks WHICH
events have already been reminded about (see RemindedCalendarEvent in
models.py) so the periodic background check (wired into main.py alongside
automation_engine's own loop) never sends the same reminder twice.

Deliberately reuses Phase 20/21's existing Twilio account
(TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN/TWILIO_FROM_NUMBER, already configured
for phone calls) rather than standing up a second Twilio integration - the
only new piece of configuration is the destination, PHONE_AGENT_REMINDER_
TO_NUMBER (Sudeep's own phone, distinct from the business line callers
dial to reach Saanvi)."""
import datetime
import os

import httpx2 as httpx
from sqlalchemy.orm import Session

from app import google_client
from app.config import settings
from app.models import GoogleAccount, RemindedCalendarEvent


def reminder_engine_configured() -> bool:
    """True only when every piece this feature needs is actually set -
    Twilio credentials/from-number (shared with Phase 20/21's calling
    feature), a destination number for Sudeep himself, and the feature
    flag. Checked before every pass, never assumed."""
    return bool(
        settings.reminder_engine_enabled
        and settings.twilio_account_sid
        and settings.twilio_auth_token
        and os.getenv("TWILIO_FROM_NUMBER", "").strip()
        and settings.reminder_to_number
    )


def _twilio_messages_url() -> str:
    return f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Messages.json"


def send_reminder_sms(body: str) -> bool:
    """Sends one real SMS to Sudeep's configured reminder number via
    Twilio's Messages API. Returns True/False rather than raising - a
    failed send must never crash the background reminder loop; the caller
    (check_and_send_due_reminders below) only records success, so a failed
    attempt is naturally retried on the next tick instead of being silently
    marked as done."""
    from_number = os.getenv("TWILIO_FROM_NUMBER", "").strip()
    try:
        with httpx.Client(timeout=15) as client:
            response = client.post(
                _twilio_messages_url(),
                auth=(settings.twilio_account_sid, settings.twilio_auth_token),
                data={
                    "To": settings.reminder_to_number,
                    "From": from_number,
                    "Body": body[:1500],
                },
            )
    except Exception:
        return False
    return response.status_code < 400


def _format_event_time(start_raw: str, tz_name: str) -> str:
    """Formats a Calendar API start value into something readable in a text
    message, in the calendar's own real timezone - never a bare UTC/offset
    string Sudeep would have to mentally convert."""
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = datetime.timezone.utc
    try:
        dt = datetime.datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return start_raw
        return dt.astimezone(tz).strftime("%b %d, %I:%M %p")
    except (TypeError, ValueError):
        return start_raw


def check_and_send_due_reminders(db: Session) -> int:
    """One pass: for every user with a connected Google account, lists
    upcoming calendar events in the next reminder-lead-time window (plus a
    little scan buffer so a slow tick never skips one), and sends a
    reminder SMS for any event that has now entered that window and hasn't
    already been reminded about (RemindedCalendarEvent is the dedup record,
    keyed on Google's own real event id). Never raises - a Google/Twilio
    problem for one user or one event must never stop the pass for anyone
    else, the same "one bad item never blocks the batch" principle as
    automation_engine.check_and_run_due. Returns how many reminders were
    actually sent, for logging."""
    if not reminder_engine_configured():
        return 0

    lead_minutes = max(settings.reminder_lead_minutes, 1)
    now = datetime.datetime.now(datetime.timezone.utc)
    window_end = now + datetime.timedelta(minutes=lead_minutes)
    scan_start_iso = now.isoformat().replace("+00:00", "Z")
    scan_end_iso = (window_end + datetime.timedelta(minutes=10)).isoformat().replace("+00:00", "Z")

    sent = 0
    try:
        accounts = db.query(GoogleAccount).all()
    except Exception:
        return 0

    for account in accounts:
        try:
            access_token = google_client.get_valid_access_token(account, db)
            tz_name = google_client.get_calendar_timezone(access_token)
            events = google_client.list_calendar_events(access_token, scan_start_iso, scan_end_iso)
        except Exception:
            continue

        for event in events:
            event_id = event.get("id")
            start_raw = event.get("start")
            if not event_id or not start_raw:
                continue
            try:
                event_start = datetime.datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if event_start.tzinfo is None:
                # An all-day event's "start" is a bare date with no time -
                # nothing meaningful to count down to a minute-precise
                # reminder for, so it is deliberately skipped rather than
                # guessed at.
                continue
            if not (now <= event_start <= window_end):
                continue

            already = (
                db.query(RemindedCalendarEvent)
                .filter(RemindedCalendarEvent.event_id == str(event_id))
                .first()
            )
            if already is not None:
                continue

            summary = event.get("summary") or "Untitled event"
            location = event.get("location") or ""
            when = _format_event_time(str(start_raw), tz_name)
            body = f'Reminder: "{summary}" at {when}'
            if location:
                body += f" ({location})"

            if send_reminder_sms(body):
                db.add(RemindedCalendarEvent(user_id=account.user_id, event_id=str(event_id)))
                db.commit()
                sent += 1

    return sent
