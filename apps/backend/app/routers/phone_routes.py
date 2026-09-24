"""Phase 20 "Incoming phone agent" (added 2026-09-22, scoped with Sudeep via
3 AskUserQuestion questions - see progress-tracker.md and phone_agent.py's
own module docstring for the full design). Twilio calls these two endpoints
directly as plain webhooks - no Bearer token, no JARVIS login, just an HTTP
POST from Twilio's own servers, form-encoded, on every incoming call and
every subsequent turn of that call. _require_configured_and_valid is what
stands between this endpoint and anyone on the internet being able to make
JARVIS "answer" a fake call (or burn real AI-provider API calls) - it's
called first, before anything else, on every single request.

Every call's full transcript is saved as a real Conversation/Message pair
(title "📞 Phone Call: <caller> (<time>)"), the same durability principle as
every other feature in this project, so Sudeep can review any call
afterward in the normal chat UI - looked up across a call's several turns by
Twilio's own CallSid (Conversation.phone_call_sid), since each webhook is a
stateless HTTP request with no other memory of the call in progress.

Per Sudeep's explicit scoping choice, a phone caller gets ZERO access to
Tally, email/calendar, automations, custom tools, or agent consult -
generate_reply() below is called with only `messages` + `agent_context` (+
optionally web search, off by default) so every other kwarg defaults to
None/off automatically. This is deliberately NOT routed through
chat_routes.build_reply_context (which offers memory_context/skill_context
and is only as restrictive as its allow_* flags say) - a phone caller's
identity is never verified at all, unlike a logged-in chat user or an
automation Sudeep authored himself, so this router hand-assembles the
absolute minimum context rather than reusing a helper built for a different
trust level.
"""
import asyncio
import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app import phone_agent
from app.ai_provider import get_ai_provider
from app.config import settings
from app.database import get_db
from app.auth import get_current_user
from app.models import Conversation, Message, PhoneCallRecord, User
from app.schemas import PhoneCallRecordOut

router = APIRouter(prefix="/api/phone", tags=["phone"])


async def _generate_phone_reply(provider, messages: list[dict], persona: str) -> str:
    """Run blocking AI generation off the event loop with a Twilio-safe cap."""
    timeout = min(max(settings.phone_agent_reply_timeout_seconds, 1.0), 12.0)
    return await asyncio.wait_for(
        asyncio.to_thread(
            provider.generate_reply,
            messages,
            agent_context=persona,
            allow_web_search=settings.phone_agent_allow_web_search,
        ),
        timeout=timeout,
    )


def _upsert_phone_record(
    db: Session,
    owner_id: int,
    conversation_id: int,
    call_sid: str,
    caller_number: str,
) -> PhoneCallRecord:
    record = db.query(PhoneCallRecord).filter(PhoneCallRecord.call_sid == call_sid).first()
    if record is None:
        record = PhoneCallRecord(
            user_id=owner_id,
            conversation_id=conversation_id,
            call_sid=call_sid,
            caller_number=caller_number or "unknown number",
            callback_requested=False,
            is_read=False,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
    return record


def _twiml_response(xml: str) -> Response:
    return Response(content=xml, media_type="application/xml")


async def _require_configured_and_valid(request: Request, form: dict) -> "str | None":
    """Returns an error TwiML string to send back immediately if this
    request should be refused, or None if it's fine to proceed. Kept as a
    single gate function (not raised exceptions) because a Twilio webhook
    that gets back a JSON 4xx error, rather than valid TwiML, just makes
    Twilio play a generic failure tone with no explanation - every refusal
    path here still returns a real, spoken explanation via TwiML instead."""
    if not settings.phone_agent_enabled:
        return phone_agent.build_final_twiml(
            "This phone assistant is currently turned off. Goodbye.",
            settings.phone_agent_language,
            settings.phone_agent_voice,
        )
    if not phone_agent.phone_agent_configured():
        return phone_agent.build_final_twiml(
            "This assistant isn't fully set up yet. Please try again later. Goodbye.",
            settings.phone_agent_language,
            settings.phone_agent_voice,
        )
    if settings.phone_agent_validate_signature:
        signature = request.headers.get("X-Twilio-Signature", "")
        # Built from the configured public base URL, not request.url - see
        # phone_agent.py's module docstring and config.py's
        # phone_agent_public_base_url comment for why.
        expected_url = settings.phone_agent_public_base_url.rstrip("/") + request.url.path
        if not phone_agent.twilio_signature_valid(
            settings.twilio_auth_token, expected_url, form, signature
        ):
            return phone_agent.build_final_twiml(
                "Sorry, this call could not be verified. Goodbye.",
                settings.phone_agent_language,
                settings.phone_agent_voice,
            )
    return None


@router.post("/incoming")
async def incoming_call(request: Request, db: Session = Depends(get_db)) -> Response:
    """Twilio's configured Voice webhook - hit once at the start of every
    call. Creates this call's own Conversation (Twilio's CallSid is stable
    for the whole call, used as the lookup key on every later /gather
    turn), saves the greeting as the first assistant Message so the
    transcript is complete from the very first thing the caller heard, and
    starts the Gather loop."""
    form = dict((await request.form()).items())
    refusal = await _require_configured_and_valid(request, form)
    if refusal:
        return _twiml_response(refusal)

    call_sid = form.get("CallSid", "")
    caller = form.get("From", "unknown number")

    owner = db.query(User).filter(User.email == settings.phone_agent_owner_email).first()
    if not owner:
        # Configured-looking (phone_agent_configured() passed - an owner
        # email string is set) but that email doesn't match any real JARVIS
        # account. A real, distinct misconfiguration from "not configured
        # at all" above, worth its own honest message rather than a crash.
        return _twiml_response(
            phone_agent.build_final_twiml(
                "This assistant isn't fully set up yet. Please try again later. Goodbye.",
                settings.phone_agent_language,
                settings.phone_agent_voice,
            )
        )

    convo = db.query(Conversation).filter(Conversation.phone_call_sid == call_sid).first()
    if not convo:
        business_tz = phone_agent.phone_timezone(settings.phone_agent_timezone)
        timestamp = datetime.datetime.now(business_tz).strftime("%Y-%m-%d %I:%M %p %Z")
        convo = Conversation(
            user_id=owner.id,
            title=f"\U0001F4DE Phone Call: {caller} ({timestamp})",
            phone_call_sid=call_sid,
        )
        db.add(convo)
        db.commit()
        db.refresh(convo)

    _upsert_phone_record(db, owner.id, convo.id, call_sid, caller)

    greeting = phone_agent.build_greeting_text(settings.phone_agent_business_name, settings.phone_agent_greeting)
    db.add(Message(conversation_id=convo.id, role="assistant", content=greeting))
    db.commit()

    gather_action_url = settings.phone_agent_public_base_url.rstrip("/") + "/api/phone/gather"
    twiml = phone_agent.build_gather_twiml(
        greeting, gather_action_url, settings.phone_agent_language, settings.phone_agent_voice
    )
    return _twiml_response(twiml)


@router.post("/gather")
async def gather_speech(request: Request, db: Session = Depends(get_db)) -> Response:
    """Twilio's action URL for the <Gather> in every TwiML response this
    router sends - hit once per turn, with SpeechResult holding whatever
    Twilio's own speech-to-text heard the caller say. Looks the call's
    Conversation back up by CallSid (see incoming_call above), appends this
    turn, generates a reply through the SAME reply pipeline as chat but
    with no business tools (see this module's own docstring), and either
    continues the Gather loop or ends the call cleanly."""
    form = dict((await request.form()).items())
    refusal = await _require_configured_and_valid(request, form)
    if refusal:
        return _twiml_response(refusal)

    call_sid = form.get("CallSid", "")
    speech = (form.get("SpeechResult") or "").strip()

    convo = db.query(Conversation).filter(Conversation.phone_call_sid == call_sid).first()
    if not convo:
        # Genuinely shouldn't happen (incoming_call always creates one
        # first) but a webhook can arrive in an order this code didn't
        # expect - never crash on it, just end the call gracefully.
        return _twiml_response(
            phone_agent.build_final_twiml(
                "Sorry, I lost track of this call. Please call back. Goodbye.",
                settings.phone_agent_language,
                settings.phone_agent_voice,
            )
        )

    if not speech:
        # Twilio only calls this action URL with an empty SpeechResult if
        # actionOnEmptyResult were set (it isn't here) - kept as a defensive
        # fallback rather than assumed unreachable, same principle as every
        # other "this shouldn't happen, but don't crash if it does" branch
        # in this project.
        return _twiml_response(
            phone_agent.build_final_twiml(
                "Sorry, I didn't catch that. Goodbye.",
                settings.phone_agent_language,
                settings.phone_agent_voice,
            )
        )

    db.add(Message(conversation_id=convo.id, role="user", content=speech))
    db.commit()

    history = (
        db.query(Message)
        .filter(Message.conversation_id == convo.id)
        .order_by(Message.created_at)
        .all()
    )

    # Safety cap on call length (PHONE_AGENT_MAX_TURNS pairs of turns) -
    # checked BEFORE calling the AI provider so a runaway/very long call
    # never makes one extra billed API call it's just going to discard.
    if len(history) >= settings.phone_agent_max_turns * 2:
        reply_text = "I need to let you go now - please call back if you need anything else. Goodbye!"
        end_call = True
    else:
        messages = [{"role": m.role, "content": m.content} for m in history]
        persona = phone_agent.build_phone_persona_context(
            settings.phone_agent_business_name, settings.phone_agent_persona
        )
        provider = get_ai_provider()
        try:
            raw_reply = await _generate_phone_reply(provider, messages, persona)
        except asyncio.TimeoutError:
            # Twilio stops waiting for slow webhook responses. Return valid
            # TwiML promptly so the caller hears a graceful explanation
            # instead of Twilio's generic "application error" recording.
            raw_reply = (
                "I'm sorry, my response is taking too long right now. Please "
                f"call back in a moment. {phone_agent.END_CALL_MARKER}"
            )
        except Exception:
            # Never let a raw exception reach a live phone call - same
            # "unexpected failure here must never crash the caller-facing
            # path" principle as automation_engine.run_one and every other
            # unattended entry point in this project.
            raw_reply = (
                "I'm having some trouble right now - please try calling back in "
                f"a moment. {phone_agent.END_CALL_MARKER}"
            )
        reply_text, callback_request = phone_agent.extract_callback_request_marker(raw_reply)
        if callback_request is not None:
            record = _upsert_phone_record(
                db,
                convo.user_id,
                convo.id,
                call_sid,
                form.get("From", "unknown number"),
            )
            record.caller_name = callback_request["caller_name"]
            record.reason = callback_request["reason"]
            record.preferred_callback_time = callback_request["preferred_time"]
            record.callback_requested = True
            # A completed callback request is always unread, even if the
            # owner happened to mark the initial call notification read
            # while the call was still in progress.
            record.is_read = False
            db.commit()
        reply_text, end_call = phone_agent.extract_end_call_marker(reply_text)
        reply_text = phone_agent.truncate_for_speech(reply_text)

    db.add(Message(conversation_id=convo.id, role="assistant", content=reply_text))
    db.commit()

    if end_call:
        twiml = phone_agent.build_final_twiml(
            reply_text, settings.phone_agent_language, settings.phone_agent_voice
        )
    else:
        gather_action_url = settings.phone_agent_public_base_url.rstrip("/") + "/api/phone/gather"
        twiml = phone_agent.build_gather_twiml(
            reply_text, gather_action_url, settings.phone_agent_language, settings.phone_agent_voice
        )
    return _twiml_response(twiml)

@router.get("/records", response_model=list[PhoneCallRecordOut])
def list_phone_records(
    unread_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(PhoneCallRecord).filter(PhoneCallRecord.user_id == current_user.id)
    if unread_only:
        query = query.filter(PhoneCallRecord.is_read.is_(False))
    return query.order_by(PhoneCallRecord.created_at.desc()).limit(25).all()


@router.post("/records/{record_id}/read", response_model=PhoneCallRecordOut)
def mark_phone_record_read(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    record = (
        db.query(PhoneCallRecord)
        .filter(
            PhoneCallRecord.id == record_id,
            PhoneCallRecord.user_id == current_user.id,
        )
        .first()
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Phone call record not found")
    record.is_read = True
    db.commit()
    db.refresh(record)
    return record

