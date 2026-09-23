import datetime
import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from app.config import settings
from app.database import SessionLocal, get_db
from app.models import User, Conversation, Message, Memory, Skill, Agent, CustomTool, Automation, GoogleAccount
from app.schemas import (
    ChatMessageIn, ChatMessageOut, ConversationOut, TallyBillDraft, TallyTaxLine, CalendarEventDraft,
)
from app.auth import get_current_user
from app.ai_provider import get_ai_provider
from app import tally_client
from app import debug_agent
from app import health_check
from app import custom_tools as custom_tools_module
from app import automation_engine
from app import google_client

router = APIRouter(prefix="/api/chat", tags=["chat"])

_TALLY_DRAFT_MARKER = "[TALLY_BILL_DRAFT]"


def _make_tally_daybook_query(company: str, host: str, port: int, timeout: int):
    """Builds the callable passed to generate_reply() as tally_daybook_query
    (see ai_provider.py's tool-use loop) - actually runs
    tally_client.fetch_daybook() against Sudeep's real Tally and formats the
    result as plain text for the model to read back to him. Never raises:
    the tool loop in ai_provider.py already wraps this in a try/except as a
    second line of defense, but every real failure mode (unreachable Tally,
    an unrecognized response shape, a genuinely empty range) is handled here
    directly so the model gets a clear, specific explanation either way."""
    def _query(date_from: str, date_to: str) -> str:
        try:
            ok, result = tally_client.fetch_daybook(host, port, timeout, company, date_from, date_to)
        except tally_client.TallyError as e:
            return f"Couldn't reach Tally: {e}"

        if not ok:
            return (
                f"Tally's response for {date_from} to {date_to} wasn't in the "
                f"expected shape (this read feature hasn't been fully "
                f"live-validated yet - see progress-tracker.md): {result}"
            )
        if not result:
            return f"No vouchers found in Tally between {date_from} and {date_to}."

        lines = []
        for v in result:
            d = v["date"]
            pretty_date = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d
            # amount (added 2026-09-20, live bug fix): v.get("amount") is
            # None whenever Tally's response didn't include a readable party
            # AMOUNT for this voucher - say so explicitly rather than
            # omitting the field, since an omitted field is exactly what let
            # the model fabricate a number the first time this shipped (see
            # tally_client.py's note on this same date). Never round/guess
            # here - amount is either the real figure or a stated "unknown".
            amount = v.get("amount")
            amount_text = f"Rs {amount:,.2f}" if amount is not None else "amount unknown (not returned by this query)"
            lines.append(
                f"- {pretty_date} | {v['voucher_type']} #{v['voucher_number']} | "
                f"{v['party_name']} | {amount_text} | {v['narration']}"
            )
        return (
            f"{len(result)} voucher(s) in Tally between {date_from} and {date_to}:\n"
            + "\n".join(lines)
        )

    return _query


def _make_tally_bill_payment_query(company: str, host: str, port: int, timeout: int):
    """Builds the callable passed to generate_reply() as
    tally_bill_payment_query (see ai_provider.py's tool-use loop) - runs
    tally_client.fetch_bill_payment_status(), a deterministic Python
    computation (never model reasoning), and formats the result as plain
    text. Added 2026-09-20 after JARVIS fabricated an entire bill record
    (wrong date, wrong party, invented reference numbers) and a wrong
    payment verdict when asked "has bill 086 been paid" and left to
    free-reason over a query_tally_daybook dump - Sudeep caught it live
    against Tally's own Day Book screen (see progress-tracker.md). Never
    raises - every real failure mode is handled here directly so the model
    gets a clear, specific explanation either way."""
    def _query(bill_number: str) -> str:
        try:
            ok, result = tally_client.fetch_bill_payment_status(host, port, timeout, company, bill_number)
        except tally_client.TallyError as e:
            return f"Couldn't reach Tally: {e}"

        if not ok:
            return f"Tally's response wasn't in the expected shape: {result}"

        status = result["status"]
        if status == "invalid_input":
            return (
                f"\"{bill_number}\" doesn't look like a bill number I can search for - "
                "ask Sudeep for the exact bill/invoice number."
            )
        if status == "not_found":
            return (
                f"No sales bill matching \"{bill_number}\" was found anywhere in Tally's "
                "records - double check the bill number with Sudeep, it may be mistyped."
            )

        bill = result["bill_vouchers"][0]
        d = bill["date"]
        pretty_date = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d
        bill_amount = bill.get("amount")
        bill_amount_text = f"Rs {bill_amount:,.2f}" if bill_amount is not None else "amount unknown"
        lines = [
            f"Bill {bill['voucher_number']} ({bill['voucher_type']}, {pretty_date}, "
            f"{bill['party_name']}, {bill_amount_text}):"
        ]
        if status == "amount_unknown":
            lines.append(
                "Found the bill but couldn't read its amount from Tally, so payment "
                "status can't be determined for certain."
            )
        elif status == "unpaid":
            lines.append("STATUS: NOT PAID - no matching payment receipt found in Tally.")
        elif status == "partially_paid":
            lines.append(
                f"STATUS: PARTIALLY PAID - Rs {result['total_received']:,.2f} of "
                f"Rs {result['total_invoice_amount']:,.2f} received so far."
            )
        else:
            lines.append(f"STATUS: PAID IN FULL - Rs {result['total_received']:,.2f} received.")

        if result["matched_receipts"]:
            lines.append("Matching receipt(s):")
            for r in result["matched_receipts"]:
                rd = r["date"]
                r_pretty_date = f"{rd[:4]}-{rd[4:6]}-{rd[6:8]}" if len(rd) == 8 else rd
                r_amount = r.get("amount")
                r_amount_text = f"Rs {r_amount:,.2f}" if r_amount is not None else "amount unknown"
                lines.append(
                    f"  - {r['voucher_number']} ({r_pretty_date}, {r_amount_text}) - "
                    f"\"{r['narration']}\" [matched by: {r['matched_by']}]"
                )

        if len(result["bill_vouchers"]) > 1:
            lines.append(
                f"NOTE: {len(result['bill_vouchers'])} different vouchers in Tally share "
                "this exact bill number - tell Sudeep this looks like a duplicate "
                "voucher-numbering issue worth checking directly in Tally."
            )

        return "\n".join(lines)

    return _query


def _make_custom_tool_invoke(db: Session, agent_tools: list[CustomTool]):
    """Phase 16 "Tool/plugin architecture" (added 2026-09-20): builds the
    dispatcher callable passed to generate_reply() as custom_tool_invoke -
    looks up the right CustomTool by its "custom_tool_<id>" tool name (see
    custom_tools.build_tool_schema) among the agent's own assigned tools
    (never any other tool, even one Sudeep owns, so a model can't call a
    tool it wasn't actually offered) and runs it. Never raises - a bad
    lookup or a real call failure both come back as plain text (see
    custom_tools.invoke_custom_tool for the call itself)."""
    by_name = {f"custom_tool_{t.id}": t for t in agent_tools}

    def _invoke(tool_name: str, params: dict) -> str:
        tool = by_name.get(tool_name)
        if tool is None:
            return "That tool isn't available to this agent."
        return custom_tools_module.invoke_custom_tool(tool, params)

    return _invoke


_MAX_CONSULT_ANSWER_CHARS = 4000


def _build_recent_phone_call_context(db: Session, user_id: int, limit: int = 5) -> str:
    """Build an authoritative, read-only summary of recent Twilio calls.

    Phone calls are ordinary Conversation/Message rows, but normal chat
    previously never received them as context. That made JARVIS incorrectly
    claim it could not check calls even immediately after answering one.
    Keep the summary deliberately small so routine chats are not flooded
    with old transcripts.
    """
    calls = (
        db.query(Conversation)
        .filter(
            Conversation.user_id == user_id,
            Conversation.phone_call_sid.isnot(None),
        )
        .order_by(Conversation.created_at.desc())
        .limit(limit)
        .all()
    )
    if not calls:
        return (
            "Authoritative incoming-phone-call records: no incoming calls "
            "have been saved for this JARVIS account yet. If Sudeep asks "
            "whether anyone called, say no saved calls were found; never "
            "substitute email results for phone-call records."
        )

    lines = [
        "Authoritative incoming-phone-call records from JARVIS's database.",
        "When Sudeep asks about calls, callers, messages, or callback requests, "
        "answer from these records and never say you cannot check phone calls.",
    ]
    remaining = 6000
    for call in calls:
        created = call.created_at.strftime("%Y-%m-%d %H:%M") if call.created_at else "time unknown"
        header = f"\nCALL: {call.title or 'Incoming phone call'} | received {created}"
        if len(header) > remaining:
            break
        lines.append(header)
        remaining -= len(header)

        messages = (
            db.query(Message)
            .filter(Message.conversation_id == call.id)
            .order_by(Message.created_at)
            .all()
        )
        for message in messages:
            speaker = "Caller" if message.role == "user" else "JARVIS"
            content = (message.content or "").strip()
            if not content:
                continue
            row = f"- {speaker}: {content}"
            if len(row) > remaining:
                lines.append("- (older transcript content omitted)")
                remaining = 0
                break
            lines.append(row)
            remaining -= len(row)
        if remaining <= 0:
            break

    return "\n".join(lines)


def run_agent_subquery(db: Session, user_id: int, question: str, target_agent: Agent) -> str:
    """Phase 17 "AI agent orchestration" (added 2026-09-20, scoped with
    Sudeep to "single-hop consult only" - see progress-tracker.md). Runs a
    single-turn, history-less sub-question against target_agent's own
    persona, deliberately WITHOUT any of Tally/debug/health-check/custom
    tools or another consult_agent tool - a conservative v1 scope, since
    Sudeep never directly reviews this intermediate exchange the way he
    reviews a normal chat reply (see config.py's agent_consult_enabled).
    Never raises - a failure here becomes a plain-text result so the
    consulting agent's own reply can still complete."""
    try:
        provider = get_ai_provider()
        sub_context = _agent_context_block(target_agent)
        reply = provider.generate_reply(
            [{"role": "user", "content": question}],
            agent_context=sub_context,
            allow_web_search=target_agent.allow_web_search,
        )
        if len(reply) > _MAX_CONSULT_ANSWER_CHARS:
            reply = reply[:_MAX_CONSULT_ANSWER_CHARS] + "... (truncated)"
        return reply
    except Exception as e:
        return f"Couldn't get an answer from \"{target_agent.name}\": {e}"


def _build_consult_directory(db: Session, user_id: int, exclude_agent_id: int | None) -> str | None:
    """Added 2026-09-20 (same-day follow-up, once Sudeep asked how to
    actually test the consult feature): without this, the model has no way
    to know any OTHER agent's name or role unless Sudeep spells it out
    himself every time - which defeats the point of an "automatic" consult
    that's supposed to trigger on the model's own judgment. Lists every
    other active agent's real name and role_description, the only two
    fields it needs to decide "does this question fit them better than me" -
    never their system_instructions or tool permissions, which aren't
    relevant to that decision and shouldn't leak between agents' personas."""
    others = (
        db.query(Agent)
        .filter(
            Agent.user_id == user_id,
            Agent.status == "active",
            Agent.id != (exclude_agent_id if exclude_agent_id is not None else -1),
        )
        .order_by(Agent.name)
        .all()
    )
    if not others:
        return None
    lines = [
        "Other agents you could consult (via consult_agent) if a question clearly fits one of them better "
        "than your own role:"
    ]
    for a in others:
        lines.append(f"- \"{a.name}\": {a.role_description}")
    return "\n".join(lines)


def _make_consult_agent_query(db: Session, user_id: int, exclude_agent_id: int | None, consulted_names: list[str]):
    """Builds the callable passed to generate_reply() as consult_agent_query.
    exclude_agent_id keeps an agent from "consulting" itself. consulted_names
    is a list this closure appends to (mutated as a side effect) so the
    caller (send_message below) can guarantee disclosure in the final reply
    even if the model forgets to mention it itself - a code-level guarantee,
    not just a prompt instruction, matching this project's usual "never
    trust the model alone for something that matters" standard."""
    def _query(agent_name: str, question: str) -> str:
        agent_name = (agent_name or "").strip()
        if not agent_name:
            return "No agent name was given to consult."
        target = (
            db.query(Agent)
            .filter(
                Agent.user_id == user_id,
                Agent.status == "active",
                func.lower(Agent.name) == agent_name.lower(),
            )
            .first()
        )
        if target is None or target.id == exclude_agent_id:
            return (
                f"\"{agent_name}\" isn't a different, active agent that can be "
                "consulted - double-check the exact name with Sudeep."
            )
        result = run_agent_subquery(db, user_id, question, target)
        if target.name not in consulted_names:
            consulted_names.append(target.name)
        return result

    return _query


def _make_create_automation(db: Session, user_id: int, agent_id: "int | None"):
    """Phase 18 "Automation" (added 2026-09-20): builds the callable passed
    to generate_reply() as create_automation_action. agent_id is whichever
    agent (if any) THIS reply is being generated under - carried onto the
    new/updated Automation row so it replays with the same persona/tool
    permissions every time it runs (see automation_engine.run_one). Reusing
    an existing name (case-insensitive, scoped to this user) updates that
    automation in place - including re-enabling a disabled one - rather
    than creating a duplicate, since that's the more forgiving behavior if
    Sudeep just wants to change a time or re-word an instruction. Never
    raises on a bad schedule - compute_next_due's ValueError is caught and
    turned into a plain-language explanation the model can relay."""
    def _create(
        name: str,
        instruction: str,
        schedule_type: str,
        time_of_day: str,
        day_of_week: "str | None" = None,
        run_date: "str | None" = None,
    ) -> str:
        # Every attempt is logged unconditionally (added 2026-09-21, see
        # automation_engine.log_create_attempt's docstring for why) - this
        # is the only way a future silent-failure case can be told apart
        # from the model simply never having called this tool at all.
        raw_args = {
            "name": name, "instruction": instruction, "schedule_type": schedule_type,
            "time_of_day": time_of_day, "day_of_week": day_of_week, "run_date": run_date,
        }
        try:
            result = _create_inner(name, instruction, schedule_type, time_of_day, day_of_week, run_date)
        except Exception as e:
            # Broadened 2026-09-21 from only catching compute_next_due's
            # ValueError - a real DB-layer failure (or anything else) used
            # to propagate all the way to ai_provider.py's generic tool
            # wrapper uncaught here, meaning it was never logged anywhere
            # permanent (ai_provider.py's own except-and-stringify hides it
            # from the app-wide exception handler too - see the log
            # function's docstring). Roll back so a half-applied change to
            # an existing row is never left dangling.
            db.rollback()
            automation_engine.log_create_attempt(user_id, agent_id, raw_args, "error", str(e))
            return f"Couldn't schedule that automation - {e}"
        automation_engine.log_create_attempt(user_id, agent_id, raw_args, "success", result)
        return result

    def _create_inner(
        name: str,
        instruction: str,
        schedule_type: str,
        time_of_day: str,
        day_of_week: "str | None",
        run_date: "str | None",
    ) -> str:
        name = (name or "").strip()
        instruction = (instruction or "").strip()
        if not name:
            return "Give this automation a short name before scheduling it."
        if not instruction:
            return "Say what should actually happen each time this runs before scheduling it."

        schedule_type = (schedule_type or "").strip().lower()
        day_of_week_norm = day_of_week.strip().lower() if day_of_week else None

        next_due = automation_engine.compute_next_due(
            schedule_type, time_of_day, day_of_week_norm, run_date, after=datetime.datetime.now()
        )

        existing = (
            db.query(Automation)
            .filter(Automation.user_id == user_id, func.lower(Automation.name) == name.lower())
            .first()
        )
        if existing is not None:
            existing.instruction = instruction
            existing.schedule_type = schedule_type
            existing.time_of_day = time_of_day
            existing.day_of_week = day_of_week_norm
            existing.run_date = run_date
            existing.agent_id = agent_id
            existing.enabled = True
            existing.next_due_at = next_due
            automation = existing
            verb = "Updated"
        else:
            automation = Automation(
                user_id=user_id,
                agent_id=agent_id,
                name=name,
                instruction=instruction,
                schedule_type=schedule_type,
                day_of_week=day_of_week_norm,
                time_of_day=time_of_day,
                run_date=run_date,
                enabled=True,
                next_due_at=next_due,
            )
            db.add(automation)
            verb = "Scheduled"
        db.commit()
        db.refresh(automation)

        return (
            f"{verb} \"{name}\" - {automation_engine.describe_schedule(automation)}. "
            f"Next run: {next_due.strftime('%Y-%m-%d %H:%M')} (only actually fires while "
            "JARVIS is open - if it's closed at that time, it runs as soon as it's "
            "reopened instead)."
        )

    return _create


def _make_list_automations(db: Session, user_id: int):
    """Builds the callable passed to generate_reply() as
    list_automations_action - includes disabled automations too (labeled as
    such) so Sudeep/the model can see the full picture, e.g. before deciding
    whether to re-enable one by recreating it.

    Also surfaces the outcome of the automation's last run (last_run_at/
    last_status/last_result_summary), which are recorded on the Automation
    row by automation_engine.run_one() but were previously write-only - the
    only way Sudeep could ever learn what a completed automation actually
    said or whether it errored was this listing, and before this fix even
    this didn't show it. Discovered as a real gap during Phase 18 live
    testing on 2026-09-20 (there was no other way, chat or UI, to see an
    automation's result after the fact) and fixed the same day."""
    def _list() -> str:
        automations = (
            db.query(Automation)
            .filter(Automation.user_id == user_id)
            .order_by(Automation.enabled.desc(), Automation.next_due_at)
            .all()
        )
        if not automations:
            return "No automations are set up yet."
        lines = []
        for a in automations:
            state = "enabled" if a.enabled else "disabled"
            next_run = a.next_due_at.strftime("%Y-%m-%d %H:%M") if a.next_due_at else "-"
            lines.append(
                f"- \"{a.name}\" ({state}) - {automation_engine.describe_schedule(a)}. "
                f"Next run: {next_run}. Instruction: {a.instruction}"
            )
            if a.last_run_at is not None:
                last_run = a.last_run_at.strftime("%Y-%m-%d %H:%M")
                status = a.last_status or "unknown"
                summary = a.last_result_summary or "(no summary recorded)"
                lines.append(
                    f"  Last run: {last_run} - {status}. Result: {summary}"
                )
        return "\n".join(lines)

    return _list


def _make_cancel_automation(db: Session, user_id: int):
    """Builds the callable passed to generate_reply() as
    cancel_automation_action. Turns the automation off (enabled=False,
    next_due_at cleared) rather than deleting its row - keeps its history/
    name around in case Sudeep wants it back via create_automation later,
    matching this project's general "never delete without being asked"
    caution."""
    def _cancel(name: str) -> str:
        name = (name or "").strip()
        if not name:
            return "Give the exact name of the automation to turn off."
        automation = (
            db.query(Automation)
            .filter(Automation.user_id == user_id, func.lower(Automation.name) == name.lower())
            .first()
        )
        if automation is None:
            return f"No automation named \"{name}\" was found - use list_automations to check the exact name."
        if not automation.enabled:
            return f"\"{automation.name}\" is already turned off."
        automation.enabled = False
        automation.next_due_at = None
        db.commit()
        return f"Turned off \"{automation.name}\" - it won't run again unless it's scheduled again."

    return _cancel


def _get_google_account(db: Session, user_id: int) -> "GoogleAccount | None":
    return db.query(GoogleAccount).filter(GoogleAccount.user_id == user_id).first()


def _make_search_emails_query(db: Session, user_id: int):
    """Builds the callable passed to generate_reply() as search_emails_query
    (Phase 19 "Email + Calendar", see google_client.search_emails). Looks up
    Sudeep's connected GoogleAccount fresh on every call (rather than once
    when build_reply_context runs) so a token refreshed mid-conversation is
    always the current one - same reasoning as every other Tally/automation
    callable factory in this file that closes over `db`/`user_id` instead of
    a snapshot."""
    def _query(query: str, max_results: int) -> str:
        account = _get_google_account(db, user_id)
        if account is None:
            return "Google isn't connected yet - Sudeep needs to connect it from Settings first."
        query = (query or "").strip()
        if not query:
            return "Give a search query, e.g. \"from:someone@example.com\" or \"is:unread\"."
        access_token = google_client.get_valid_access_token(account, db)
        results = google_client.search_emails(access_token, query, max_results)
        if not results:
            return f"No emails matched \"{query}\"."
        lines = [
            f"- id={r['id']} | from: {r['from']} | subject: {r['subject']} | {r['date']} | {r['snippet']}"
            for r in results
        ]
        return f"{len(results)} email(s) matching \"{query}\":\n" + "\n".join(lines)

    return _query


def _make_read_email_query(db: Session, user_id: int):
    """Builds the callable passed to generate_reply() as read_email_query
    (see google_client.get_email)."""
    def _query(message_id: str) -> str:
        account = _get_google_account(db, user_id)
        if account is None:
            return "Google isn't connected yet - Sudeep needs to connect it from Settings first."
        message_id = (message_id or "").strip()
        if not message_id:
            return "Give the email's id (from a search_emails result)."
        access_token = google_client.get_valid_access_token(account, db)
        email = google_client.get_email(access_token, message_id)
        return (
            f"From: {email['from']}\nTo: {email['to']}\nSubject: {email['subject']}\n"
            f"Date: {email['date']}\nThread id: {email['thread_id']}\n\n{email['body']}"
        )

    return _query


def _make_draft_email_reply_query(db: Session, user_id: int):
    """Builds the callable passed to generate_reply() as
    draft_email_reply_query (see google_client.create_draft_reply). Creates a
    REAL Gmail draft - deliberately called live with no separate chat-side
    confirmation step, since a draft has zero external effect (see
    google_client.py's module docstring and JARVIS_SYSTEM_PROMPT for the full
    reasoning: it sits inert in Sudeep's own Gmail Drafts folder until he
    presses Send there himself)."""
    def _query(to: str, subject: str, body: str, thread_id: "str | None") -> str:
        account = _get_google_account(db, user_id)
        if account is None:
            return "Google isn't connected yet - Sudeep needs to connect it from Settings first."
        to = (to or "").strip()
        if not to:
            return "Give a recipient email address for the draft."
        access_token = google_client.get_valid_access_token(account, db)
        result = google_client.create_draft_reply(access_token, thread_id, to, subject or "", body or "")
        return (
            f"Created a draft (id={result['draft_id']}) in Sudeep's Gmail Drafts folder, to {to}, "
            f"subject \"{subject}\". Nothing was sent - he needs to open Gmail himself to review and "
            "send it."
        )

    return _query


def _local_day_bounds_to_utc_iso(date_from: str, date_to: str, tz_name: str) -> "tuple[str, str] | None":
    """Turns a plain YYYY-MM-DD..YYYY-MM-DD range into real RFC3339 UTC
    timestamps for the START of date_from and the END of date_to, in the
    calendar's own timezone (tz_name, from google_client.get_calendar_
    timezone) - so "today" means Sudeep's actual local day, not a UTC day
    that could be off by several hours. Returns None on an unparseable
    date, so the caller can give a clear error instead of a confusing one
    from the Calendar API."""
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(tz_name)
        start = datetime.datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=tz)
        end = (datetime.datetime.strptime(date_to, "%Y-%m-%d") + datetime.timedelta(days=1)).replace(tzinfo=tz)
    except Exception:
        return None
    start_utc = start.astimezone(datetime.timezone.utc)
    end_utc = end.astimezone(datetime.timezone.utc)
    return start_utc.isoformat().replace("+00:00", "Z"), end_utc.isoformat().replace("+00:00", "Z")


def _make_list_calendar_events_query(db: Session, user_id: int):
    """Builds the callable passed to generate_reply() as
    list_calendar_events_query (see google_client.list_calendar_events)."""
    def _query(date_from: str, date_to: str) -> str:
        account = _get_google_account(db, user_id)
        if account is None:
            return "Google isn't connected yet - Sudeep needs to connect it from Settings first."
        access_token = google_client.get_valid_access_token(account, db)
        tz_name = google_client.get_calendar_timezone(access_token)
        bounds = _local_day_bounds_to_utc_iso(date_from, date_to, tz_name)
        if bounds is None:
            return f"\"{date_from}\"/\"{date_to}\" don't look like valid YYYY-MM-DD dates."
        time_min, time_max = bounds
        events = google_client.list_calendar_events(access_token, time_min, time_max)
        if not events:
            return f"No calendar events found between {date_from} and {date_to}."
        lines = [
            f"- {e['start']} to {e['end']} | {e['summary']}" + (f" | {e['location']}" if e["location"] else "")
            for e in events
        ]
        return f"{len(events)} event(s) between {date_from} and {date_to}:\n" + "\n".join(lines)

    return _query


def _make_find_open_slots_query(db: Session, user_id: int):
    """Builds the callable passed to generate_reply() as
    find_open_slots_query. The actual free/busy interval comes straight from
    Google Calendar's own freeBusy endpoint (google_client.find_free_busy) -
    the real, deterministic gap-finding (subtracting busy blocks from a
    9:00-19:00 local working-hours window each day, per this project's
    "never trust the AI's arithmetic" principle - see e.g. the Tally GST tax
    computation) happens entirely in this function, in real Python, never
    left to the model to work out from a raw busy-interval dump."""
    def _query(date_from: str, date_to: str, duration_minutes: int) -> str:
        account = _get_google_account(db, user_id)
        if account is None:
            return "Google isn't connected yet - Sudeep needs to connect it from Settings first."
        try:
            duration_minutes = int(duration_minutes or 30)
        except (TypeError, ValueError):
            duration_minutes = 30
        if duration_minutes <= 0:
            duration_minutes = 30

        access_token = google_client.get_valid_access_token(account, db)
        tz_name = google_client.get_calendar_timezone(access_token)
        bounds = _local_day_bounds_to_utc_iso(date_from, date_to, tz_name)
        if bounds is None:
            return f"\"{date_from}\"/\"{date_to}\" don't look like valid YYYY-MM-DD dates."
        time_min, time_max = bounds
        busy_raw = google_client.find_free_busy(access_token, time_min, time_max)

        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
        busy = []
        for b in busy_raw:
            try:
                busy.append((
                    datetime.datetime.fromisoformat(b["start"].replace("Z", "+00:00")).astimezone(tz),
                    datetime.datetime.fromisoformat(b["end"].replace("Z", "+00:00")).astimezone(tz),
                ))
            except (KeyError, ValueError):
                continue
        busy.sort()

        day_start_hour, day_end_hour = 9, 19  # a sensible default business-hours window
        duration = datetime.timedelta(minutes=duration_minutes)
        start_date = datetime.datetime.strptime(date_from, "%Y-%m-%d").date()
        end_date = datetime.datetime.strptime(date_to, "%Y-%m-%d").date()

        slots = []
        day = start_date
        while day <= end_date:
            window_start = datetime.datetime.combine(day, datetime.time(day_start_hour, 0), tzinfo=tz)
            window_end = datetime.datetime.combine(day, datetime.time(day_end_hour, 0), tzinfo=tz)
            cursor = window_start
            day_busy = [b for b in busy if b[0] < window_end and b[1] > window_start]
            for b_start, b_end in day_busy:
                if b_start - cursor >= duration:
                    slots.append((cursor, b_start))
                cursor = max(cursor, b_end)
            if window_end - cursor >= duration:
                slots.append((cursor, window_end))
            day += datetime.timedelta(days=1)
            if len(slots) >= 15:
                break

        if not slots:
            return (
                f"No free {duration_minutes}-minute slot found between {date_from} and {date_to} "
                f"(within a {day_start_hour}:00-{day_end_hour}:00 working-hours window each day)."
            )
        lines = [f"- {s.strftime('%Y-%m-%d %H:%M')} to {e.strftime('%H:%M')}" for s, e in slots[:15]]
        return (
            f"Free {duration_minutes}-minute slot(s) between {date_from} and {date_to} "
            f"(within a {day_start_hour}:00-{day_end_hour}:00 working-hours window each day):\n"
            + "\n".join(lines)
        )

    return _query


_CALENDAR_DRAFT_MARKER = "[CALENDAR_EVENT_DRAFT]"


def _extract_calendar_draft(reply_text: str) -> tuple[str, dict | None]:
    """Modeled directly on _extract_tally_draft below - looks for a
    [CALENDAR_EVENT_DRAFT]{...json...} block, pulls the JSON out by matching
    braces, and returns the reply text with that block removed plus the
    parsed dict (or the original text and None if there was no block, or it
    didn't parse). Never raises: a malformed draft just means no review
    card, not a broken chat reply."""
    idx = reply_text.find(_CALENDAR_DRAFT_MARKER)
    if idx == -1:
        return reply_text, None

    start = idx + len(_CALENDAR_DRAFT_MARKER)
    while start < len(reply_text) and reply_text[start].isspace():
        start += 1
    if start >= len(reply_text) or reply_text[start] != "{":
        return reply_text, None

    depth = 0
    end = None
    for i in range(start, len(reply_text)):
        ch = reply_text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        return reply_text, None

    try:
        data = json.loads(reply_text[start:end])
    except json.JSONDecodeError:
        return reply_text, None

    cleaned = (reply_text[:idx] + reply_text[end:]).strip()
    return cleaned, data


def _extract_tally_draft(reply_text: str) -> tuple[str, dict | None]:
    """Looks for a [TALLY_BILL_DRAFT]{...json...} block in the reply (see
    ai_provider.py's system prompt for what puts it there), pulls the JSON
    out by matching braces (a plain regex would mishandle the nested braces
    in "items"/"tax_lines"), and returns the reply text with that block
    removed plus the parsed dict - or the original text and None if there
    was no block, or it didn't parse as valid JSON. Never raises: a
    malformed draft just means no draft, not a broken chat reply."""
    idx = reply_text.find(_TALLY_DRAFT_MARKER)
    if idx == -1:
        return reply_text, None

    start = idx + len(_TALLY_DRAFT_MARKER)
    while start < len(reply_text) and reply_text[start].isspace():
        start += 1
    if start >= len(reply_text) or reply_text[start] != "{":
        return reply_text, None

    depth = 0
    end = None
    for i in range(start, len(reply_text)):
        ch = reply_text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        return reply_text, None

    try:
        data = json.loads(reply_text[start:end])
    except json.JSONDecodeError:
        return reply_text, None

    cleaned = (reply_text[:idx] + reply_text[end:]).strip()
    return cleaned, data


def _agent_context_block(agent: Agent) -> str:
    """Agent Factory v1: the system-prompt overlay that makes a reply come
    from this specific named agent's persona rather than general JARVIS -
    see the Agent model's docstring in models.py. Kept short and direct
    since it's prepended before memory/skill/tally context, not replacing
    JARVIS_SYSTEM_PROMPT."""
    lines = [
        f"For this conversation, you are acting as \"{agent.name}\", a specific "
        "role Sudeep set up for you (you are still JARVIS underneath - never "
        "claim to be a different AI). Your role:",
        agent.role_description,
    ]
    if agent.system_instructions:
        lines.append(agent.system_instructions)
    lines.append(
        "Stay focused on this role. If Sudeep asks for something clearly "
        "outside it, you can still help, but you don't need to mention the "
        "role restriction unless it's genuinely relevant."
    )
    return "\n\n".join(lines)


def _update_memories(user_id: int, user_message: str, assistant_reply: str) -> None:
    """Phase 9: runs as a FastAPI background task, AFTER the chat reply has
    already been sent back to the user, so memory extraction never adds
    latency to a visible reply. Uses its own DB session rather than the
    request's, since the request's session may already be closed by the
    time a background task runs."""
    db = SessionLocal()
    try:
        provider = get_ai_provider()
        existing = (
            db.query(Memory)
            .filter(Memory.user_id == user_id)
            .order_by(Memory.updated_at.desc())
            .limit(settings.memory_max_recall)
            .all()
        )
        existing_payload = [
            {"id": m.id, "content": m.content, "category": m.category} for m in existing
        ]
        result = provider.extract_memories(existing_payload, user_message, assistant_reply)

        for item in result.get("new", []):
            content = (item.get("content") or "").strip()
            if content:
                db.add(
                    Memory(
                        user_id=user_id,
                        content=content,
                        category=item.get("category"),
                        source="auto",
                    )
                )

        for item in result.get("updates", []):
            memory = (
                db.query(Memory)
                .filter(Memory.id == item.get("id"), Memory.user_id == user_id)
                .first()
            )
            new_content = (item.get("content") or "").strip()
            if memory and new_content:
                memory.content = new_content
                if item.get("category"):
                    memory.category = item["category"]

        db.commit()
    except Exception:
        # Memory extraction is best-effort background work - never let it
        # surface an error anywhere, and never leave a half-written commit.
        db.rollback()
    finally:
        db.close()


def build_reply_context(
    db: Session,
    user_id: int,
    agent: "Agent | None",
    *,
    allow_tally: bool = True,
    allow_automation_management: bool = True,
    allow_email_calendar: bool = True,
) -> dict:
    """Builds every optional generate_reply() kwarg (memory/skill/tally/
    agent-persona context, and every client-side tool callable) for a given
    agent (or None for plain JARVIS chat). Factored out of send_message
    (added 2026-09-20, alongside Phase 18 "Automation") so the interactive
    chat endpoint below and automation_engine.run_one's unattended runs can
    never drift apart on what context/tools a reply gets - one place
    decides it, not two copies that could silently diverge over time.

    allow_tally=False (used by automation_engine, since Sudeep chose
    "read-only actions only, no approval needed" for Phase 18 v1 - see
    progress-tracker.md) forces tally_context/tally_daybook_query/
    tally_bill_payment_query off regardless of the agent's own allow_
    tally_billing setting - only ever MORE restrictive than the interactive
    endpoint's own per-agent setting, never less.

    allow_automation_management=False (also used by automation_engine)
    withholds the create_automation/list_automations/cancel_automation
    tools entirely, so an automation's own unattended run can never create,
    change, or cancel automations on its own - those tools only exist in a
    live, Sudeep-initiated conversation.

    allow_email_calendar=False (Phase 19 "Email + Calendar", also used by
    automation_engine, same conservative-by-default choice already made for
    allow_tally above - an unattended automation run never gets read/draft
    email or calendar access, even though search/read/list/find-slots are
    themselves read-only, since draft_email_reply does create a real (if
    inert) Gmail draft and this project's standard is to keep an automation's
    toolbox strictly no-side-effects) forces every email/calendar tool off
    regardless of the agent's own allow_email_calendar setting - only ever
    MORE restrictive than the interactive endpoint's own per-agent setting,
    never less.

    Returns {"kwargs": {...ready to **-splat into generate_reply()...},
    "consulted_names": [...mutated as a side effect by the consult_agent_
    query callable inside kwargs, same as send_message's own local variable
    used to be...], "agent_allows_tally": bool (for the caller's own
    defense-in-depth check on any [TALLY_BILL_DRAFT] that comes back),
    "agent_allows_email_calendar": bool (same defense-in-depth idea, for any
    [CALENDAR_EVENT_DRAFT] that comes back)}."""
    # Phase 9: pull in whatever JARVIS already remembers about Sudeep so it
    # can use it naturally in this reply, the same way a real assistant would
    # recall something you'd told them in an earlier conversation.
    memory_context = None
    if settings.memory_enabled:
        memories = (
            db.query(Memory)
            .filter(Memory.user_id == user_id)
            .order_by(Memory.updated_at.desc())
            .limit(settings.memory_max_recall)
            .all()
        )
        if memories:
            lines = "\n".join(f"- ({m.category or 'general'}) {m.content}" for m in memories)
            memory_context = (
                "What you remember about Sudeep from earlier conversations "
                "(use naturally when relevant; don't recite this list back "
                "verbatim unless he asks what you remember):\n" + lines
            )

    # Phase 11 (Universal Skill Engine, v1): fold in whatever approved skills
    # Sudeep has learned, the same way memories are folded in above. Only
    # status="active" skills are ever used here - a pending_review skill
    # JARVIS just researched is invisible to chat until Sudeep approves it
    # on the Skills page (see skill_routes.py).
    skill_context = None
    if settings.skill_engine_enabled:
        # Agent Factory v1: an agent with a non-empty assigned_skill_ids list
        # only draws on that subset of Sudeep's approved skills, not
        # everything active - see the Agent model's docstring. An empty list
        # means "no skills assigned", not "all skills" (an agent that should
        # draw on everything just doesn't need this restriction at all), so
        # that case skips the query entirely rather than filtering by an
        # empty IN(...) list.
        skills = []
        if agent is not None:
            assigned_ids = json.loads(agent.assigned_skill_ids or "[]")
            if assigned_ids:
                skills = (
                    db.query(Skill)
                    .filter(
                        Skill.user_id == user_id,
                        Skill.status == "active",
                        Skill.id.in_(assigned_ids),
                    )
                    .order_by(Skill.approved_at.desc())
                    .limit(settings.skill_max_active_recall)
                    .all()
                )
        else:
            skills = (
                db.query(Skill)
                .filter(Skill.user_id == user_id, Skill.status == "active")
                .order_by(Skill.approved_at.desc())
                .limit(settings.skill_max_active_recall)
                .all()
            )
        if skills:
            cap = settings.skill_context_chars_per_skill
            blocks = []
            for s in skills:
                body = s.content if len(s.content) <= cap else s.content[:cap] + "... (truncated)"
                blocks.append(f"### {s.name}\n{body}")
            skill_context = (
                "Skills you've learned and Sudeep approved (use naturally when "
                "relevant; each is a full reference, not something to recite "
                "back verbatim unless asked):\n\n" + "\n\n".join(blocks)
            )

    # Custom Tally billing feature: tell JARVIS the actual configured ledger
    # names (Sales + IGST/CGST/SGST) so it uses Sudeep's real books rather
    # than inventing a name - always included when the feature is on, the
    # same way skill_context is always included when there are active skills.
    # Agent Factory v1: an agent with allow_tally_billing=False never sees
    # the ledger names or gets asked to draft a bill at all - this is enforced
    # again in send_message (force-discarding any tally_draft that somehow
    # still shows up in the reply anyway) as defense-in-depth against a model
    # that ignores the missing context or a prompt-injection attempt.
    # allow_tally=False (Phase 18 automation runs) forces this off entirely,
    # regardless of the agent's own setting - see this function's docstring.
    agent_allows_tally = (agent.allow_tally_billing if agent is not None else True) and allow_tally
    tally_context = None
    if settings.tally_enabled and agent_allows_tally:
        tally_context = (
            "Tally billing ledger names configured for this business (use "
            "these exact names when drafting a [TALLY_BILL_DRAFT] block - "
            "never invent your own):\n"
            f"- Sales ledger: \"{settings.tally_sales_ledger}\"\n"
            f"- IGST ledger (interstate bills): \"{settings.tally_igst_ledger}\"\n"
            f"- CGST ledger (same-state bills): \"{settings.tally_cgst_ledger}\"\n"
            f"- SGST ledger (same-state bills): \"{settings.tally_sgst_ledger}\""
        )

    agent_context = _agent_context_block(agent) if agent is not None else None

    # Phase 20: make actual Twilio call records visible to ordinary JARVIS
    # chat. This is database-backed evidence, not model memory: it lets
    # questions such as "did anyone call me?" return the saved call and
    # transcript instead of an unrelated email summary or a false
    # "I cannot check phone calls" answer.
    if settings.phone_agent_enabled:
        phone_call_context = _build_recent_phone_call_context(db, user_id)
        agent_context = (
            f"{agent_context}\n\n{phone_call_context}"
            if agent_context
            else phone_call_context
        )

    allow_web_search = agent.allow_web_search if agent is not None else True

    # Tally daybook read: same gate as tally_context above (feature enabled +
    # this call allows Tally at all) - reading Sudeep's real Tally data is
    # part of the same Tally toolbox as billing, so it's withheld together.
    tally_daybook_query = None
    tally_bill_payment_query = None
    if settings.tally_enabled and agent_allows_tally and settings.tally_company_name:
        tally_daybook_query = _make_tally_daybook_query(
            settings.tally_company_name,
            settings.tally_host,
            settings.tally_port,
            settings.tally_timeout_seconds,
        )
        tally_bill_payment_query = _make_tally_bill_payment_query(
            settings.tally_company_name,
            settings.tally_host,
            settings.tally_port,
            settings.tally_timeout_seconds,
        )

    # Phase 13 "Debugging Agent": unlike the Tally tools above, this is NOT
    # gated by agent_allows_tally or any per-agent flag - every conversation
    # (agent or plain JARVIS chat, interactive or an automation run) gets the
    # same read-only introspection into JARVIS's own errors/source, on or
    # off only by the single settings.debug_agent_enabled switch.
    debug_read_errors = None
    debug_list_files = None
    debug_read_source = None
    if settings.debug_agent_enabled:
        debug_read_errors = debug_agent.read_recent_errors
        debug_list_files = debug_agent.list_backend_source_files
        debug_read_source = debug_agent.read_backend_source

    # Phase 14 "Self-diagnostics": same "not gated per-agent" v1 choice as
    # the debug tools above.
    health_check_query = health_check.run_and_format_health_check if settings.health_check_enabled else None

    # Phase 16 "Tool/plugin architecture": unlike the built-in tools above,
    # custom tools ARE gated per-agent by design - only offered at all in an
    # agent conversation, and only this specific agent's own assigned_
    # custom_tool_ids. Plain JARVIS chat (agent is None) never gets any.
    custom_tool_specs = None
    custom_tool_invoke = None
    if settings.tool_registry_enabled and agent is not None:
        assigned_tool_ids = json.loads(agent.assigned_custom_tool_ids or "[]")
        agent_tools = custom_tools_module.get_agent_custom_tools(db, user_id, assigned_tool_ids)
        if agent_tools:
            custom_tool_specs = [custom_tools_module.build_tool_schema(t) for t in agent_tools]
            custom_tool_invoke = _make_custom_tool_invoke(db, agent_tools)

    # Phase 17 "AI agent orchestration": a single-hop consult, offered
    # whenever there's at least one OTHER active agent to consult - not
    # gated per-agent beyond that. consulted_names is mutated by the
    # callable itself (see _make_consult_agent_query) so a disclosure
    # footer is a real code-level guarantee, not dependent on the model
    # remembering to mention it.
    consult_agent_query = None
    consult_directory_context = None
    consulted_names: list[str] = []
    if settings.agent_consult_enabled:
        exclude_id = agent.id if agent is not None else None
        consult_directory_context = _build_consult_directory(db, user_id, exclude_id)
        if consult_directory_context is not None:
            consult_agent_query = _make_consult_agent_query(db, user_id, exclude_id, consulted_names)

    # Phase 18 "Automation" (added 2026-09-20): create/list/cancel are
    # offered only when explicitly allowed (allow_automation_management=
    # True, the default) - an automation's own unattended run
    # (automation_engine.run_one) passes False, so it can never manage
    # automations on its own. Not gated per-agent, same v1 choice already
    # made for the debug/health-check tools above.
    create_automation_action = None
    list_automations_action = None
    cancel_automation_action = None
    if settings.automation_engine_enabled and allow_automation_management:
        agent_id_for_automation = agent.id if agent is not None else None
        create_automation_action = _make_create_automation(db, user_id, agent_id_for_automation)
        list_automations_action = _make_list_automations(db, user_id)
        cancel_automation_action = _make_cancel_automation(db, user_id)

    # Phase 19 "Email + Calendar" (added 2026-09-22): search_emails/read_email/
    # draft_email_reply/list_calendar_events/find_open_slots are only ever
    # offered when the feature is on, Sudeep actually has a connected
    # GoogleAccount, AND this call is allowed to use it (agent_allows_email_
    # calendar below, mirroring agent_allows_tally's exact reasoning) - a
    # plain check for "is the feature enabled" alone would offer tools that
    # immediately fail on every call for an agent that hasn't connected
    # Google, or dish out email access to an agent Sudeep never opted in.
    agent_allows_email_calendar = (
        agent.allow_email_calendar if agent is not None else True
    ) and allow_email_calendar
    search_emails_query = None
    read_email_query = None
    draft_email_reply_query = None
    list_calendar_events_query = None
    find_open_slots_query = None
    if settings.email_calendar_enabled and agent_allows_email_calendar:
        google_account = _get_google_account(db, user_id)
        if google_account is not None:
            search_emails_query = _make_search_emails_query(db, user_id)
            read_email_query = _make_read_email_query(db, user_id)
            draft_email_reply_query = _make_draft_email_reply_query(db, user_id)
            list_calendar_events_query = _make_list_calendar_events_query(db, user_id)
            find_open_slots_query = _make_find_open_slots_query(db, user_id)

    return {
        "kwargs": {
            "memory_context": memory_context,
            "skill_context": skill_context,
            "tally_context": tally_context,
            "agent_context": agent_context,
            "allow_web_search": allow_web_search,
            "tally_daybook_query": tally_daybook_query,
            "tally_bill_payment_query": tally_bill_payment_query,
            "debug_read_errors": debug_read_errors,
            "debug_list_files": debug_list_files,
            "debug_read_source": debug_read_source,
            "health_check_query": health_check_query,
            "custom_tool_specs": custom_tool_specs,
            "custom_tool_invoke": custom_tool_invoke,
            "consult_agent_query": consult_agent_query,
            "consult_directory_context": consult_directory_context,
            "create_automation_action": create_automation_action,
            "list_automations_action": list_automations_action,
            "cancel_automation_action": cancel_automation_action,
            "search_emails_query": search_emails_query,
            "read_email_query": read_email_query,
            "draft_email_reply_query": draft_email_reply_query,
            "list_calendar_events_query": list_calendar_events_query,
            "find_open_slots_query": find_open_slots_query,
        },
        "consulted_names": consulted_names,
        "agent_allows_tally": agent_allows_tally,
        "agent_allows_email_calendar": agent_allows_email_calendar,
    }


@router.post("/message", response_model=ChatMessageOut)
def send_message(
    payload: ChatMessageIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if payload.conversation_id:
        conversation = (
            db.query(Conversation)
            .filter(Conversation.id == payload.conversation_id, Conversation.user_id == current_user.id)
            .first()
        )
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
    else:
        # Agent Factory v1: agent_id on the payload only matters when
        # STARTING a brand-new conversation - it's fixed on the Conversation
        # row from here on (see schemas.py's ChatMessageIn docstring and the
        # Agent model in models.py). A paused agent can't start a new
        # conversation, but existing conversations tied to it keep working -
        # that check happens only here, not on the resume path below.
        if payload.agent_id is not None:
            if not settings.agent_factory_enabled:
                raise HTTPException(status_code=403, detail="Agent Factory is disabled")
            requested_agent = (
                db.query(Agent)
                .filter(Agent.id == payload.agent_id, Agent.user_id == current_user.id)
                .first()
            )
            if not requested_agent:
                raise HTTPException(status_code=404, detail="Agent not found")
            if requested_agent.status != "active":
                raise HTTPException(
                    status_code=400,
                    detail=f"\"{requested_agent.name}\" is paused - resume it before starting a new chat",
                )

        conversation = Conversation(
            user_id=current_user.id,
            title=payload.message[:50],
            agent_id=payload.agent_id,
        )
        db.add(conversation)
        db.commit()
        db.refresh(conversation)

    # Load the conversation's own agent (if any) - once set at creation this
    # governs every later message in the conversation regardless of what
    # agent_id (if any) is sent along with them. A paused agent still works
    # for a conversation that already exists (only starting NEW ones is
    # blocked, above).
    agent = None
    if conversation.agent_id is not None:
        agent = db.query(Agent).filter(Agent.id == conversation.agent_id).first()

    user_message = Message(conversation_id=conversation.id, role="user", content=payload.message)
    db.add(user_message)
    db.commit()

    history = (
        db.query(Message)
        .filter(Message.conversation_id == conversation.id)
        .order_by(Message.created_at)
        .all()
    )
    ai_messages = [{"role": m.role, "content": m.content} for m in history]

    ctx = build_reply_context(db, current_user.id, agent, allow_tally=True, allow_automation_management=True)
    consulted_names = ctx["consulted_names"]
    agent_allows_tally = ctx["agent_allows_tally"]
    agent_allows_email_calendar = ctx["agent_allows_email_calendar"]

    provider = get_ai_provider()
    reply_text = provider.generate_reply(ai_messages, **ctx["kwargs"])

    # Phase 17 disclosure guarantee (added 2026-09-20): the system prompt
    # already asks the model to mention any agent it consulted, but this
    # project's standard is never to rely on the model alone for something
    # that matters (see e.g. the Tally payment-status tool's own history) -
    # so if consulted_names is non-empty, a plain footer is appended
    # regardless of what the model already said, guaranteeing Sudeep always
    # sees when another agent was actually involved.
    if consulted_names:
        footer = "(Consulted: " + ", ".join(consulted_names) + ")"
        reply_text = f"{reply_text}\n\n{footer}"

    # Custom Tally billing feature: if JARVIS decided it has enough detail to
    # draft a bill, its reply carries a [TALLY_BILL_DRAFT]{...} block. Strip
    # it out here so neither the saved conversation history nor the memory
    # background task ever sees the raw marker - only the clean, human
    # reply. The parsed draft (if any) goes back to the frontend separately
    # as tally_draft for it to render as a review card.
    reply_text, tally_draft_data = _extract_tally_draft(reply_text)
    tally_draft = None
    # Defense-in-depth: if this is an agent conversation with billing turned
    # off, discard any draft outright even though tally_context was already
    # withheld above - a model that hallucinates the marker anyway (or is
    # steered into it by injected content) still can't produce a usable
    # review card.
    if tally_draft_data is not None and not agent_allows_tally:
        tally_draft_data = None
    if tally_draft_data is not None:
        try:
            tally_draft = TallyBillDraft(**tally_draft_data)
        except Exception:
            # A malformed draft just means no review card is shown - never
            # let it break the chat reply itself.
            tally_draft = None

        # If JARVIS gave a rate + interstate/intrastate instead of exact
        # tax_lines (the normal path - see ai_provider.py), compute the real
        # tax amounts here too so the review card shows Sudeep the actual
        # numbers before he ever clicks "Send to Tally". This is only a
        # preview - create_bill (tally_routes.py) always recomputes this
        # itself as the authoritative step, never trusting whatever the
        # frontend echoes back.
        if tally_draft is not None and tally_draft.gst_rate is not None and tally_draft.tax_type and not tally_draft.tax_lines:
            try:
                items_total = round(sum(i.amount for i in tally_draft.items), 2)
                computed = tally_client.compute_gst_tax_lines(
                    items_total,
                    tally_draft.gst_rate,
                    tally_draft.tax_type,
                    settings.tally_igst_ledger,
                    settings.tally_cgst_ledger,
                    settings.tally_sgst_ledger,
                )
                tally_draft.tax_lines = [
                    TallyTaxLine(ledger=t["ledger"], amount=t["amount"]) for t in computed
                ]
            except Exception:
                # A bad rate/tax_type just means the review card shows no tax
                # preview - create_bill will raise a clear error if Sudeep
                # tries to send it, rather than crashing the chat reply here.
                pass

    # Phase 19 "Email + Calendar": same pattern as the Tally draft handling
    # above, for a [CALENDAR_EVENT_DRAFT]{...} block - stripped from the
    # saved history/memory background task, parsed separately for the
    # frontend's review card. Defense-in-depth discard mirrors the Tally
    # draft's own: even though the calendar tools/system-prompt instructions
    # were already withheld above when agent_allows_email_calendar is False,
    # a model that hallucinates the marker anyway still can't produce a
    # usable review card.
    reply_text, calendar_draft_data = _extract_calendar_draft(reply_text)
    calendar_draft = None
    if calendar_draft_data is not None and not agent_allows_email_calendar:
        calendar_draft_data = None
    if calendar_draft_data is not None:
        try:
            calendar_draft = CalendarEventDraft(**calendar_draft_data)
        except Exception:
            # A malformed draft just means no review card is shown - never
            # let it break the chat reply itself.
            calendar_draft = None

    assistant_message = Message(conversation_id=conversation.id, role="assistant", content=reply_text)
    db.add(assistant_message)
    db.commit()

    if settings.memory_enabled:
        background_tasks.add_task(_update_memories, current_user.id, payload.message, reply_text)

    return {
        "conversation_id": conversation.id,
        "reply": reply_text,
        "tally_draft": tally_draft,
        "calendar_draft": calendar_draft,
        "agent_id": agent.id if agent is not None else None,
        "agent_name": agent.name if agent is not None else None,
    }


@router.get("/conversations", response_model=list[ConversationOut])
def list_conversations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return (
        db.query(Conversation)
        .filter(Conversation.user_id == current_user.id)
        .order_by(Conversation.created_at.desc())
        .all()
    )
