"""Phase 21/22 outbound phone foundation.

This router adds the first safe outbound-call action for JARVIS: an
authenticated user can approve one call, to one number, with one clear
opening message/purpose. The endpoint does not let the model place calls by
itself; the frontend or API caller must explicitly POST the approved draft.

The live call then reuses the existing Phase 20 Twilio gather loop. The
conversation row is created immediately after Twilio accepts the call, using
Twilio's returned CallSid, so later /api/phone/gather webhooks append the
callee's replies to the same transcript just like incoming calls.
"""
import datetime
import os

import httpx2 as httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import phone_agent
from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models import Conversation, Message, User

router = APIRouter(prefix="/api/phone", tags=["phone"])


class OutboundCallIn(BaseModel):
    to_number: str = Field(..., min_length=6, max_length=32)
    purpose: str = Field("", max_length=500)
    opening_message: str = Field(..., min_length=10, max_length=700)


class OutboundCallOut(BaseModel):
    success: bool
    call_sid: str
    status: str
    message: str
    conversation_id: int | None = None


def _twilio_from_number() -> str:
    return os.getenv("TWILIO_FROM_NUMBER", "").strip()


def _outbound_calls_enabled() -> bool:
    return os.getenv("PHONE_AGENT_OUTBOUND_ENABLED", "true").lower() == "true"


def _twilio_calls_url() -> str:
    return f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Calls.json"


def _require_outbound_ready() -> str:
    if not settings.phone_agent_enabled:
        raise HTTPException(status_code=403, detail="Phone agent is turned off")
    if not _outbound_calls_enabled():
        raise HTTPException(status_code=403, detail="Outbound phone calls are disabled")
    if not phone_agent.phone_agent_configured():
        raise HTTPException(status_code=400, detail="Phone agent is not fully configured")
    from_number = _twilio_from_number()
    if not from_number:
        raise HTTPException(status_code=400, detail="TWILIO_FROM_NUMBER is not configured")
    return from_number


@router.post("/outbound-call", response_model=OutboundCallOut)
def place_outbound_call(
    payload: OutboundCallIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Place one approved outbound Twilio call.

    This is intentionally a direct action endpoint, not an AI tool. JARVIS
    can draft or suggest what to say, but the real call only starts when an
    authenticated UI/API request posts the final number and opening message.
    """
    from_number = _require_outbound_ready()
    to_number = payload.to_number.strip()
    opening_message = phone_agent.truncate_for_speech(payload.opening_message.strip(), max_chars=700)
    if not to_number:
        raise HTTPException(status_code=400, detail="Give the number to call")

    gather_action_url = settings.phone_agent_public_base_url.rstrip("/") + "/api/phone/gather"
    twiml = phone_agent.build_gather_twiml(
        opening_message,
        gather_action_url,
        settings.phone_agent_language,
        settings.phone_agent_voice,
    )

    try:
        with httpx.Client(timeout=15) as client:
            response = client.post(
                _twilio_calls_url(),
                auth=(settings.twilio_account_sid, settings.twilio_auth_token),
                data={
                    "To": to_number,
                    "From": from_number,
                    "Twiml": twiml,
                },
            )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Twilio: {exc}") from exc

    if response.status_code >= 400:
        detail = "Twilio rejected the outbound call"
        try:
            body = response.json()
            detail = body.get("message") or detail
        except Exception:
            if response.text:
                detail = response.text[:300]
        raise HTTPException(status_code=502, detail=detail)

    data = response.json()
    call_sid = data.get("sid") or ""
    status = data.get("status") or "queued"
    if not call_sid:
        raise HTTPException(status_code=502, detail="Twilio did not return a call SID")

    business_tz = phone_agent.phone_timezone(settings.phone_agent_timezone)
    timestamp = datetime.datetime.now(business_tz).strftime("%Y-%m-%d %I:%M %p %Z")
    purpose = payload.purpose.strip() or "approved outbound call"
    conversation = Conversation(
        user_id=current_user.id,
        title=f"📞 Outbound Call: {to_number} ({timestamp})",
        phone_call_sid=call_sid,
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    db.add(Message(conversation_id=conversation.id, role="assistant", content=opening_message))
    db.add(Message(conversation_id=conversation.id, role="assistant", content=f"Outbound call purpose: {purpose}"))
    db.commit()

    return OutboundCallOut(
        success=True,
        call_sid=call_sid,
        status=status,
        message="Outbound call started. The transcript will continue in JARVIS if the person speaks.",
        conversation_id=conversation.id,
    )
