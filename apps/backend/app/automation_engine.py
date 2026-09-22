"""Phase 18 "Automation" v1 (added 2026-09-20, scoped with Sudeep via
AskUserQuestion - see progress-tracker.md and models.py's Automation
docstring). This module owns everything about actually RUNNING a scheduled
automation and computing when it's next due: chat_routes.py owns the
create/list/cancel tool callables Sudeep talks to (the Automation rows
themselves), and main.py owns wiring this module's check_and_run_due() into
a startup catch-up pass plus a periodic background loop.

Deliberately does NOT import chat_routes.py at module load time (even
though run_one() needs chat_routes.build_reply_context() to actually
generate a reply) - chat_routes.py imports THIS module (for compute_next_
due/describe_schedule, used by the create/list tool callables), so a
top-level import in both directions would deadlock. run_one() imports
chat_routes lazily instead (a plain function-local import), which is safe
because by the time run_one() is actually called both modules have already
finished loading."""

import datetime as dt
import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import Automation, Agent, Conversation, Message

# How overdue next_due_at has to be (compared to when a run actually
# happens) before the run is labeled "late" in the trigger message Sudeep
# sees. A few minutes of drift is normal (JARVIS was already open, the
# periodic check just hasn't ticked yet) and not worth flagging - only a
# real "JARVIS was closed" gap is called out.
LATE_THRESHOLD_MINUTES = 15

# How much of a run's reply to keep as Automation.last_result_summary, so
# list_automations can show a short preview without the full text.
_SUMMARY_MAX_CHARS = 280

_WEEKDAY_INDEX = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_WEEKDAY_NAMES = list(_WEEKDAY_INDEX.keys())


def _parse_time_of_day(value: str) -> tuple[int, int]:
    parts = (value or "").strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"\"{value}\" isn't a valid time - use 24-hour HH:MM, e.g. \"08:00\"")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"\"{value}\" isn't a valid time - use 24-hour HH:MM, e.g. \"08:00\"")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"\"{value}\" isn't a valid 24-hour time")
    return hour, minute


def _parse_run_date(value: str) -> dt.date:
    try:
        return dt.datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        raise ValueError(f"\"{value}\" isn't a valid date - use YYYY-MM-DD")


def compute_next_due(
    schedule_type: str,
    time_of_day: str,
    day_of_week: "str | None",
    run_date: "str | None",
    after: dt.datetime,
) -> dt.datetime:
    """The core scheduling math, deliberately kept as plain, deterministic
    Python (never left to the model) - same "never trust the model alone
    for something that matters" standard as the Tally tax computation and
    bill-payment-status tools. `after` is always what a fresh datetime.now()
    at the moment of computing gave the caller - never re-derived here -
    so this function is trivially testable with a fixed clock."""
    hour, minute = _parse_time_of_day(time_of_day)

    if schedule_type == "once":
        if run_date:
            d = _parse_run_date(run_date)
            return dt.datetime(d.year, d.month, d.day, hour, minute)
        candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= after:
            candidate += dt.timedelta(days=1)
        return candidate

    if schedule_type == "daily":
        candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= after:
            candidate += dt.timedelta(days=1)
        return candidate

    if schedule_type == "weekly":
        if not day_of_week:
            raise ValueError("A weekly automation needs a day_of_week")
        target_idx = _WEEKDAY_INDEX.get(day_of_week.strip().lower())
        if target_idx is None:
            raise ValueError(
                f"\"{day_of_week}\" isn't a day of the week - use monday..sunday"
            )
        candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        days_ahead = (target_idx - candidate.weekday()) % 7
        candidate += dt.timedelta(days=days_ahead)
        if candidate <= after:
            candidate += dt.timedelta(days=7)
        return candidate

    raise ValueError(f"Unknown schedule_type: \"{schedule_type}\" - use once, daily, or weekly")


# Permanent diagnostic log for every create_automation attempt (added
# 2026-09-21). Added after two real, unexplained live cases - a "Catch-up
# Test" automation and, the same day, an "Anti-Fabrication Final Check"
# automation - where JARVIS confidently told Sudeep an automation was
# scheduled, but it never actually showed up in list_automations or fired.
# Its own debug tools (read_recent_backend_errors) found nothing, because
# ai_provider.py's tool loop already catches any exception from
# create_automation_action and turns it into a tool_result string rather
# than letting it reach the app-wide exception handler - so a real failure
# there was invisible to every existing log. This logs every attempt
# unconditionally (not just failures) so a future case can show definitively
# whether _create() was ever actually called and what happened inside it,
# rather than relying on inference. Same capped-JSONL, best-effort pattern
# as tally_client.py's log_tally_failure/_log_daybook_query.
_CREATE_LOG_PATH = Path(__file__).parent / "automation_create_log.jsonl"
_CREATE_LOG_MAX_ENTRIES = 200


def log_create_attempt(user_id: int, agent_id: "int | None", args: dict, outcome: str, detail: str) -> None:
    """outcome is one of "success"/"error"; detail is the returned message
    (success) or the exception text (error). Best-effort - a logging
    failure here must never break automation creation."""
    try:
        entry = {
            "timestamp": dt.datetime.now().isoformat(),
            "user_id": user_id,
            "agent_id": agent_id,
            "args": args,
            "outcome": outcome,
            "detail": detail,
        }
        lines: list[str] = []
        if _CREATE_LOG_PATH.exists():
            lines = _CREATE_LOG_PATH.read_text(encoding="utf-8").splitlines()
        lines.append(json.dumps(entry))
        lines = lines[-_CREATE_LOG_MAX_ENTRIES:]
        _CREATE_LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def describe_schedule(automation: Automation) -> str:
    """Plain-language schedule description for list_automations and the
    create/update confirmation message."""
    if automation.schedule_type == "once":
        return f"once on {automation.run_date} at {automation.time_of_day}"
    if automation.schedule_type == "daily":
        return f"daily at {automation.time_of_day}"
    if automation.schedule_type == "weekly":
        day = (automation.day_of_week or "").capitalize()
        return f"weekly on {day} at {automation.time_of_day}"
    return automation.schedule_type


def run_one(db: Session, automation: Automation) -> None:
    """Actually runs one due automation: builds a dedicated conversation for
    it on first run, generates a reply to its instruction (read-only tools
    only - see chat_routes.build_reply_context's allow_tally=False,
    allow_automation_management=False, allow_email_calendar=False), appends both sides as ordinary
    Messages, and advances the schedule. Never raises - a failure (a bad
    instruction, a provider error, anything) is caught and recorded as the
    automation's own last_status/last_result_summary and as the assistant
    message Sudeep sees, exactly the same "never let one bad run break
    anything else" standard as check_and_run_due's own per-automation
    try/except below."""
    # Imported here, not at module load time - see this module's own
    # docstring for why (breaks a circular import with chat_routes.py).
    from app.routers import chat_routes

    now = dt.datetime.now()
    due_at = automation.next_due_at
    late = due_at is not None and (now - due_at).total_seconds() > LATE_THRESHOLD_MINUTES * 60

    conversation = None
    if automation.conversation_id is not None:
        conversation = (
            db.query(Conversation).filter(Conversation.id == automation.conversation_id).first()
        )
    if conversation is None:
        conversation = Conversation(
            user_id=automation.user_id,
            title=f"\U0001F501 Automation: {automation.name}",
            agent_id=automation.agent_id,
        )
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
        automation.conversation_id = conversation.id

    agent = None
    if automation.agent_id is not None:
        agent = db.query(Agent).filter(Agent.id == automation.agent_id).first()

    trigger_note = f"[Automation “{automation.name}”] {automation.instruction}"
    if late:
        trigger_note += (
            f" (this was due at {due_at.strftime('%Y-%m-%d %H:%M')} - JARVIS wasn't "
            "running then, so it's running now instead)"
        )
    db.add(Message(conversation_id=conversation.id, role="user", content=trigger_note))
    db.commit()

    try:
        ctx = chat_routes.build_reply_context(
            db, automation.user_id, agent, allow_tally=False, allow_automation_management=False,
            allow_email_calendar=False,
        )
        provider = chat_routes.get_ai_provider()
        reply_text = provider.generate_reply(
            [{"role": "user", "content": automation.instruction}], **ctx["kwargs"]
        )
        if ctx["consulted_names"]:
            reply_text = f"{reply_text}\n\n(Consulted: {', '.join(ctx['consulted_names'])})"
        # Defense-in-depth: allow_tally=False/allow_email_calendar=False
        # already withhold the Tally and email/calendar tools/context above,
        # so this should never actually find a [TALLY_BILL_DRAFT] or
        # [CALENDAR_EVENT_DRAFT] marker - stripped anyway on the same
        # principle as chat_routes.send_message's own equivalent step.
        reply_text, _ = chat_routes._extract_tally_draft(reply_text)
        reply_text, _ = chat_routes._extract_calendar_draft(reply_text)
        status = "success"
    except Exception as e:
        reply_text = f"This automation didn't run successfully: {e}"
        status = "error"

    db.add(Message(conversation_id=conversation.id, role="assistant", content=reply_text))

    automation.last_run_at = now
    automation.last_status = status
    automation.last_result_summary = (
        reply_text if len(reply_text) <= _SUMMARY_MAX_CHARS
        else reply_text[:_SUMMARY_MAX_CHARS] + "..."
    )

    if automation.schedule_type == "once":
        automation.enabled = False
        automation.next_due_at = None
    else:
        automation.next_due_at = compute_next_due(
            automation.schedule_type,
            automation.time_of_day,
            automation.day_of_week,
            automation.run_date,
            after=now + dt.timedelta(minutes=1),
        )
    db.commit()


def check_and_run_due(db: Session) -> int:
    """Runs every enabled automation whose next_due_at has passed. Used both
    for the startup catch-up pass and the periodic background loop (see
    main.py) - the exact same function either way, since "catch up" and
    "still open when it comes due" are the same check, just at different
    times. Returns how many ran. One automation's failure (caught inside
    run_one, but this is a second line of defense in case something outside
    run_one's own try/except still goes wrong - e.g. the initial query
    itself) never stops the others from running."""
    now = dt.datetime.now()
    due = (
        db.query(Automation)
        .filter(
            Automation.enabled == True,  # noqa: E712 - SQLAlchemy filter, not a Python bool check
            Automation.next_due_at.isnot(None),
            Automation.next_due_at <= now,
        )
        .all()
    )
    ran = 0
    for automation in due:
        try:
            run_one(db, automation)
            ran += 1
        except Exception:
            db.rollback()
    return ran
