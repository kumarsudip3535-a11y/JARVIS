"""Phase 20 "Incoming phone agent" (added 2026-09-22, scoped with Sudeep via
3 AskUserQuestion questions - see progress-tracker.md). Sudeep's choices:
Twilio as the telephony provider (he wanted more info first, got it, then
confirmed); v1 call scope is "live conversation, no business tools" (JARVIS
can talk naturally on the phone but gets zero access to Tally, email,
calendar, automations, custom tools, or agent consult - a caller's identity
is never verified, unlike a logged-in chat user or an automation Sudeep
authored himself); and he chose to forward his real SS Retail Services
number rather than testing on a fresh one first.

This module holds every PURE, testable piece of the phone-agent logic -
Twilio webhook signature validation, TwiML (Twilio's call-control XML)
generation, the phone-specific persona/system-prompt overlay, and the
[END_CALL] marker convention a reply uses to signal the call should end.
routers/phone_routes.py is the thin FastAPI layer wiring these to real
HTTP endpoints, a real DB conversation, and a real AI provider call - kept
separate the same way google_client.py/tally_client.py separate pure logic
from their own routers, so all of this can be unit tested without a running
server or a real Twilio account.

Design notes worth knowing before touching this:
- Twilio's webhook has NO Authorization header at all - it's a plain HTTP
  POST from Twilio's own servers. The only thing standing between this
  endpoint and anyone on the internet being able to make JARVIS "answer" a
  fake call (and burn real AI-provider API calls) is validating Twilio's
  own request signature (twilio_signature_valid below) against
  TWILIO_AUTH_TOKEN - checked before anything else runs, every request,
  unless Sudeep explicitly turns it off via PHONE_AGENT_VALIDATE_SIGNATURE
  (kept as an escape hatch only because signature validation behind a
  tunnel/proxy is a known fragile spot - see phone_routes.py).
- A phone reply is SPOKEN aloud, not read - JARVIS's persona overlay here
  explicitly instructs short, conversational answers (1-3 sentences), a
  hard departure from a normal chat reply's normal length.
- The [END_CALL] marker is the same "structured signal in the model's own
  reply text" pattern this project already uses for [TALLY_BILL_DRAFT] and
  [CALENDAR_EVENT_DRAFT] - here it's simpler (a bare marker, not a JSON
  draft) since there's nothing to review before a call just... ends.
"""
import base64
import hashlib
import hmac
from xml.sax.saxutils import escape as _xml_escape

from app.config import settings

_END_CALL_MARKER = "[END_CALL]"
# Public alias - phone_routes.py's own fallback-on-exception reply needs to
# embed this literal marker too, and importing a leading-underscore name
# across modules is the kind of thing that quietly breaks later, so it gets
# a real public name instead.
END_CALL_MARKER = _END_CALL_MARKER


def phone_agent_configured() -> bool:
    """Mirrors google_client.google_oauth_configured()'s pattern exactly -
    every value the phone agent genuinely needs to place and answer a real
    call, checked together so a caller never gets a half-configured
    experience (e.g. valid Twilio creds but no public URL to build a working
    Gather loop with)."""
    return bool(
        settings.twilio_account_sid
        and settings.twilio_auth_token
        and settings.phone_agent_owner_email
        and settings.phone_agent_public_base_url
    )


def twilio_signature_valid(auth_token: str, url: str, post_params: dict, signature_header: str) -> bool:
    """Twilio's own documented request-validation algorithm (stable, widely
    implemented, not something this project is inventing): take the exact
    URL Twilio was configured to call, append every POST parameter's key
    directly followed by its value (no separator) in ASCII-sorted key
    order, HMAC-SHA1 the result with the account's Auth Token as the key,
    base64-encode the digest, and compare it (constant-time, never a plain
    ==, to avoid a timing side-channel) to the X-Twilio-Signature header
    Twilio sent. No twilio SDK dependency needed - this is the entire
    algorithm, using only the standard library, matching this project's
    established "hand-roll a simple REST/webhook integration rather than
    pull in a heavy SDK" convention (tally_client.py, google_client.py)."""
    if not auth_token or not signature_header:
        return False
    base = url
    for key in sorted(post_params.keys()):
        base += key + str(post_params[key])
    computed = base64.b64encode(
        hmac.new(auth_token.encode("utf-8"), base.encode("utf-8"), hashlib.sha1).digest()
    ).decode("utf-8")
    return hmac.compare_digest(computed, signature_header)


def build_greeting_text(business_name: str, custom_greeting: str) -> str:
    """A custom PHONE_AGENT_GREETING always wins if Sudeep set one. The
    default is deliberately upfront that this is an AI, not a human -
    genuinely helpful and honest is the standard every other feature in
    this project already holds itself to (never claim to be something
    it isn't), and it also just sets an honest, low-friction expectation
    for the caller from the very first sentence."""
    if custom_greeting:
        return custom_greeting
    name = business_name or "this business"
    return f"Hi, thanks for calling {name}. You're speaking with an AI assistant. How can I help you today?"


def build_phone_persona_context(business_name: str, extra_persona: str) -> str:
    """The system-prompt overlay passed as generate_reply()'s agent_context
    for every phone-call reply - same mechanism Agent Factory v1's
    _agent_context_block already uses, just phone-specific. Deliberately
    spells out, explicitly, the three ways a phone call differs from a
    normal JARVIS chat reply (see this module's own docstring) rather than
    assuming the model infers them - this project's repeated experience
    (the Phase 18 fabrication saga above all) is that leaving safety-
    relevant behavior to inference rather than an explicit instruction is
    how real bugs happen."""
    name = business_name or "Sudeep's business"
    lines = [
        f'You are answering a live incoming PHONE CALL for {name}, talking with a '
        "real caller in real time - not a normal JARVIS chat reply. Three things "
        "matter here that don't apply to chat:",
        "1. Your reply is converted directly to speech and played to the caller - "
        "keep every reply SHORT and conversational (1 to 3 sentences), never a "
        "list, a long explanation, or anything written to be read rather than "
        "heard.",
        "2. You have NO access to Tally, email, calendar, automations, custom "
        "tools, or agent consult during this call - a phone caller's identity is "
        "never verified, so none of JARVIS's other business tools are available "
        "here no matter what's asked. If the caller needs something that requires "
        "one of those, say honestly you'll need Sudeep to follow up directly, and "
        "offer to take a message for him.",
        "3. If asked, always say plainly that you're an AI assistant - never "
        "claim to be a human. Be warm and genuinely helpful; for many callers "
        "this is their first impression of the business.",
        f'When the conversation has naturally wrapped up (the caller said '
        f"goodbye, or there's nothing more you can help with), end your reply "
        f"with the exact text {_END_CALL_MARKER} by itself - this is a real "
        "signal your own code uses to end the call cleanly; the caller never "
        "hears it.",
    ]
    if extra_persona:
        lines.append("Additional instructions Sudeep has set for phone calls specifically:\n" + extra_persona)
    return "\n\n".join(lines)


def extract_end_call_marker(reply_text: str) -> "tuple[str, bool]":
    """Strips the [END_CALL] marker (anywhere in the text, not strictly
    required to be trailing - tolerant of the model's own formatting
    variance) and reports whether it was present. Returns (clean_text,
    should_end_call)."""
    text = (reply_text or "").strip()
    end_call = _END_CALL_MARKER in text
    if end_call:
        text = text.replace(_END_CALL_MARKER, "").strip()
    return text, end_call


def truncate_for_speech(text: str, max_chars: int = 700) -> str:
    """A safety cap, not the primary control (the persona instruction above
    is) - in case the model ignores the "keep it short" instruction on a
    given turn. Cuts at the last sentence boundary before max_chars rather
    than a hard mid-word chop, so a spoken reply never trails off
    mid-sentence; only falls back to a hard cutoff if no reasonable
    sentence boundary exists in range."""
    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    best = -1
    for boundary in (". ", "! ", "? "):
        idx = window.rfind(boundary)
        if idx > best:
            best = idx
    if best > max_chars * 0.4:
        return window[: best + 1].strip()
    return window.rstrip() + "..."


def _say_block(text: str, voice: str, language: str) -> str:
    return f'<Say voice="{_xml_escape(voice)}" language="{_xml_escape(language)}">{_xml_escape(text)}</Say>'


def build_gather_twiml(
    prompt_text: str,
    gather_action_url: str,
    language: str,
    voice: str,
    gather_timeout: int = 6,
) -> str:
    """The TwiML for one conversational turn: speak prompt_text, then listen
    for the caller's next reply via Twilio's own built-in speech-to-text
    (input="speech" - no separate STT provider/cost needed). If Twilio
    hears nothing at all before gather_timeout, it falls through to the
    trailing <Say>+<Hangup> in this SAME response and the call ends
    gracefully - deliberately simpler than a special no-input branch in the
    /gather endpoint itself (see phone_routes.py)."""
    say = _say_block(prompt_text, voice, language)
    goodbye = _say_block("Sorry, I didn't hear anything. Goodbye.", voice, language)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Gather input="speech" action="{_xml_escape(gather_action_url)}" '
        f'method="POST" speechTimeout="auto" timeout="{gather_timeout}" '
        f'language="{_xml_escape(language)}">{say}</Gather>{goodbye}<Hangup/></Response>'
    )


def build_final_twiml(closing_text: str, language: str, voice: str) -> str:
    """Speaks closing_text once and hangs up - used both for a genuine
    [END_CALL] and for every graceful-error path (misconfigured, unknown
    call, provider failure) so a caller always hears SOMETHING sensible
    and the call ends cleanly rather than hanging or erroring out with
    Twilio's own generic failure tone."""
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{_say_block(closing_text, voice, language)}<Hangup/></Response>'
