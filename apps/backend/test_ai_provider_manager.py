"""Verifies the Multi-Model AI Brain upgrade (added 2026-09-21, scoped with
Sudeep via AskUserQuestion after he pasted the 82-section "JARVIS MASTER
UPGRADE PROMPT" - see progress-tracker.md): GeminiProvider's and
GroqProvider's own tool-use loops (mirroring test_tally_daybook_tool.py's
approach for AnthropicProvider, but against each provider's own REST
response shape), the shared _dispatch_client_tool/_offered_tool_specs/
_build_system_prompt helpers AnthropicProvider was refactored to use, the
Anthropic-native -> Gemini/Groq tool-schema translation, and
AIProviderManager's fallback-chain behavior (config-driven ordering,
skipping an unconfigured provider, falling back on a provider-level
failure, NOT falling back on an application-level one, and degrading to a
friendly message when every provider fails).

Uses a stubbed 'anthropic' module (same technique as
test_ai_provider_skills.py/test_tally_daybook_tool.py) and fake httpx
clients for Gemini/Groq (no real API key or network needed for any test
here - matches this project's standing test-suite convention of never
making a live call in an automated test)."""
import sys
import types

sys.path.insert(0, "/home/claude/jarvis_build")

fake_anthropic_module = types.SimpleNamespace(Anthropic=lambda api_key=None: None)
sys.modules["anthropic"] = fake_anthropic_module

from app.config import settings
from app.ai_provider import (
    AIProviderManager,
    AnthropicProvider,
    GeminiProvider,
    GroqProvider,
    ProviderError,
    ProviderCapabilityError,
    ProviderNotConfiguredError,
    _tool_schema_to_gemini,
    _tool_schema_to_groq,
    _dispatch_client_tool,
    _offered_tool_specs,
    HEALTH_CHECK_TOOL,
    TALLY_DAYBOOK_TOOL,
    CREATE_AUTOMATION_TOOL,
)

PASS = 0
FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS: {label}")
    else:
        FAIL += 1
        print(f"FAIL: {label} {extra}")


# ===========================================================================
# Tool schema translation (Anthropic-native -> Gemini / Groq wire formats)
# ===========================================================================

gem = _tool_schema_to_gemini(TALLY_DAYBOOK_TOOL)
check("Gemini translation keeps name/description, renames input_schema->parameters",
      gem["name"] == "query_tally_daybook"
      and gem["description"] == TALLY_DAYBOOK_TOOL["description"]
      and gem["parameters"] == TALLY_DAYBOOK_TOOL["input_schema"],
      gem)
check("Gemini translation carries the real required/enum fields through unchanged",
      gem["parameters"]["required"] == ["date_from", "date_to"], gem)

grq = _tool_schema_to_groq(CREATE_AUTOMATION_TOOL)
check("Groq translation wraps in OpenAI's {type, function: {...}} shape",
      grq["type"] == "function" and grq["function"]["name"] == "create_automation", grq)
check("Groq translation's function.parameters matches the original input_schema exactly",
      grq["function"]["parameters"] == CREATE_AUTOMATION_TOOL["input_schema"], grq)
check("Groq translation preserves the enum on schedule_type",
      grq["function"]["parameters"]["properties"]["schedule_type"]["enum"] == ["once", "daily", "weekly"],
      grq)

empty_tool = {"name": "no_schema_tool", "description": "d"}
check("translation tolerates a tool dict with no input_schema key at all (defensive default)",
      _tool_schema_to_gemini(empty_tool)["parameters"] == {"type": "object", "properties": {}},
      _tool_schema_to_gemini(empty_tool))


# ===========================================================================
# Shared _dispatch_client_tool - exercised directly (independent of which
# provider calls it), including the None-check safety fix it now protects
# for all three providers at once.
# ===========================================================================

check("a withheld-but-real tool name gets the honest 'not available' message, not a crash",
      "isn't available in this conversation" in _dispatch_client_tool("query_tally_daybook", {}),
      _dispatch_client_tool("query_tally_daybook", {}))
check("a genuinely unknown tool name gets 'Unknown tool', distinct from the withheld-tool message",
      _dispatch_client_tool("something_the_model_invented", {}) == "Unknown tool: something_the_model_invented")
check("a real callable is actually invoked with the right args extracted from tool_input",
      _dispatch_client_tool(
          "check_tally_bill_payment_status", {"bill_number": "086"},
          tally_bill_payment_query=lambda b: f"checked:{b}",
      ) == "checked:086")
check("a raising callable is caught and turned into a plain-text explanation, never propagates",
      "Couldn't cancel that automation" in _dispatch_client_tool(
          "cancel_automation", {"name": "x"},
          cancel_automation_action=lambda n: (_ for _ in ()).throw(RuntimeError("db gone")),
      ))
check("None tool_input (e.g. a tool called with no arguments) doesn't crash the dispatcher",
      _dispatch_client_tool("run_system_health_check", None, health_check_query=lambda: "ok") == "ok")


# ===========================================================================
# GeminiProvider's own tool-use loop, against Gemini's real REST response
# shape (candidates[0].content.parts[], functionCall/functionResponse) -
# mirrors test_tally_daybook_tool.py's approach for AnthropicProvider.
# ===========================================================================

class FakeHttpxResponse:
    def __init__(self, status_code, json_body):
        self.status_code = status_code
        self._json_body = json_body
        self.text = str(json_body)[:300]

    def json(self):
        return self._json_body


class FakeHttpxClient:
    """script is a list of (status_code, json_body) tuples, one per expected
    POST call."""
    def __init__(self, script):
        self.script = script
        self.calls = []

    def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        status_code, body = self.script[len(self.calls) - 1]
        return FakeHttpxResponse(status_code, body)


def make_gemini(script, model="gemini-3.8-flash"):
    p = GeminiProvider.__new__(GeminiProvider)
    p.api_key = "fake-key"
    p.model = model
    p._client = FakeHttpxClient(script)
    return p


def gemini_text_response(text):
    return (200, {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}]})


def gemini_function_call_response(name, args, call_id="toolu_1"):
    return (200, {"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {"name": name, "args": args}}]}}]})


p = make_gemini([gemini_text_response("Hi there")])
result = p.generate_reply([{"role": "user", "content": "hi"}])
check("Gemini: plain reply with no client tools offered returns the model's text directly",
      result == "Hi there" and len(p._client.calls) == 1)
check("Gemini: no 'tools' key sent in the payload when no client tool is offered",
      "tools" not in p._client.calls[0]["json"], p._client.calls[0]["json"])

queried = []
p = make_gemini([
    gemini_function_call_response("query_tally_daybook", {"date_from": "2026-09-21", "date_to": "2026-09-21"}),
    gemini_text_response("You have 2 bills today."),
])
result = p.generate_reply(
    [{"role": "user", "content": "what bills today"}],
    tally_daybook_query=lambda df, dt: queried.append((df, dt)) or "2 bills",
)
check("Gemini: query_tally_daybook is offered via functionDeclarations when the callable is given",
      "tools" in p._client.calls[0]["json"]
      and p._client.calls[0]["json"]["tools"][0]["functionDeclarations"][0]["name"] == "query_tally_daybook")
check("Gemini: the callable was actually invoked with the model's real args",
      queried == [("2026-09-21", "2026-09-21")], queried)
check("Gemini: the functionResponse round-trip produces the final text reply",
      result == "You have 2 bills today." and len(p._client.calls) == 2)
check("Gemini: the second call's contents include a functionResponse part with our result",
      any(
          "functionResponse" in part
          for turn in p._client.calls[1]["json"]["contents"]
          for part in turn.get("parts", [])
      ))

p = make_gemini([
    gemini_function_call_response("query_tally_daybook", {"date_from": "x", "date_to": "y"}),
    gemini_text_response("I don't have access to Tally right now."),
])
result = p.generate_reply(
    [{"role": "user", "content": "what bills today"}],
    tally_daybook_query=None,  # withheld this call, but the model still asks for it (ghost call)
    debug_read_errors=lambda *a, **k: "irrelevant",  # any client tool, so the loop still runs
)
_ghost_call_response_text = next(
    part["functionResponse"]["response"]["result"]
    for turn in p._client.calls[1]["json"]["contents"]
    for part in turn.get("parts", [])
    if "functionResponse" in part
)
check("Gemini: a ghost functionCall for a withheld tool gets the honest not-available message (regression guard, same NoneType-crash-era fix)",
      "isn't available in this conversation" in _ghost_call_response_text, _ghost_call_response_text)
check("Gemini: the ghost-tool round-trip still completes with the model's own follow-up reply",
      result == "I don't have access to Tally right now.")

p = make_gemini([(401, {"error": {"message": "bad key"}})])
try:
    p.generate_reply([{"role": "user", "content": "hi"}])
    check("Gemini: a 401 response raises ProviderError, not a silent/blank reply", False)
except ProviderError as e:
    check("Gemini: a 401 response is classified as AUTHENTICATION_ERROR", e.category == "AUTHENTICATION_ERROR", e.category)

p = make_gemini([(429, {"error": {"message": "rate limited"}})])
try:
    p.generate_reply([{"role": "user", "content": "hi"}])
    check("Gemini: a 429 response raises ProviderError", False)
except ProviderError as e:
    check("Gemini: a 429 response is classified as RATE_LIMIT (fallback-worthy)", e.category == "RATE_LIMIT", e.category)

p = make_gemini([(400, {"error": {"message": "bad request"}})])
try:
    p.generate_reply([{"role": "user", "content": "hi"}])
    check("Gemini: a 400 response raises ProviderError", False)
except ProviderError as e:
    check("Gemini: a 400 response is classified as INVALID_REQUEST (NOT fallback-worthy)", e.category == "INVALID_REQUEST", e.category)

p = make_gemini([gemini_text_response("")])
check("Gemini: a blank text reply falls back to the same 'try again' message AnthropicProvider uses",
      "please try again" in p.generate_reply([{"role": "user", "content": "hi"}]))


# ===========================================================================
# GroqProvider's own tool-use loop, against Groq's OpenAI-compatible REST
# response shape (choices[0].message.tool_calls[], role="tool" results).
# ===========================================================================

def make_groq(script, model="openai/gpt-oss-120b"):
    p = GroqProvider.__new__(GroqProvider)
    p.api_key = "fake-key"
    p.model = model
    p._client = FakeHttpxClient(script)
    return p


def groq_text_response(text):
    return (200, {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]})


def groq_tool_call_response(name, args_dict, call_id="call_1"):
    import json as _json
    return (200, {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": _json.dumps(args_dict)}}],
            },
            "finish_reason": "tool_calls",
        }]
    })


p = make_groq([groq_text_response("Hi there")])
result = p.generate_reply([{"role": "user", "content": "hi"}])
check("Groq: plain reply with no client tools offered returns the model's text directly",
      result == "Hi there" and len(p._client.calls) == 1)
check("Groq: system prompt is sent as the first message with role=system",
      p._client.calls[0]["json"]["messages"][0]["role"] == "system")
check("Groq: no 'tools' key sent when no client tool is offered",
      "tools" not in p._client.calls[0]["json"], p._client.calls[0]["json"])

cancelled = []
p = make_groq([
    groq_tool_call_response("cancel_automation", {"name": "Daily Briefing"}),
    groq_text_response("Turned it off."),
])
result = p.generate_reply(
    [{"role": "user", "content": "cancel daily briefing"}],
    cancel_automation_action=lambda n: cancelled.append(n) or f"Turned off \"{n}\".",
)
check("Groq: cancel_automation is offered in OpenAI tool-call shape when the callable is given",
      p._client.calls[0]["json"]["tools"][0]["function"]["name"] == "cancel_automation")
check("Groq: the tool_calls[].function.arguments JSON string was correctly parsed into real args",
      cancelled == ["Daily Briefing"], cancelled)
check("Groq: the tool result round-trip (role='tool') produces the final reply",
      result == "Turned it off." and len(p._client.calls) == 2)
check("Groq: the second call's messages include a role='tool' message carrying our result",
      any(m.get("role") == "tool" for m in p._client.calls[1]["json"]["messages"]))

p = make_groq([(500, {"error": "boom"})])
try:
    p.generate_reply([{"role": "user", "content": "hi"}])
    check("Groq: a 500 response raises ProviderError", False)
except ProviderError as e:
    check("Groq: a 500 response is classified as SERVER_ERROR (fallback-worthy)", e.category == "SERVER_ERROR", e.category)

p = make_groq([(404, {"error": "model not found"})])
try:
    p.generate_reply([{"role": "user", "content": "hi"}])
    check("Groq: a 404 response raises ProviderError", False)
except ProviderError as e:
    check("Groq: a 404 response is classified as MODEL_UNAVAILABLE", e.category == "MODEL_UNAVAILABLE", e.category)


# ===========================================================================
# GeminiProvider/GroqProvider construction requires their API key - the
# "skip a provider with no key configured" mechanism AIProviderManager
# depends on.
# ===========================================================================

_orig_gemini_key = settings.gemini_api_key
_orig_groq_key = settings.groq_api_key
settings.gemini_api_key = ""
settings.groq_api_key = ""
try:
    GeminiProvider()
    check("GeminiProvider() with no GEMINI_API_KEY raises ProviderNotConfiguredError", False)
except ProviderNotConfiguredError as e:
    check("GeminiProvider() with no GEMINI_API_KEY raises ProviderNotConfiguredError", e.provider == "gemini")
try:
    GroqProvider()
    check("GroqProvider() with no GROQ_API_KEY raises ProviderNotConfiguredError", False)
except ProviderNotConfiguredError as e:
    check("GroqProvider() with no GROQ_API_KEY raises ProviderNotConfiguredError", e.provider == "groq")
settings.gemini_api_key = _orig_gemini_key
settings.groq_api_key = _orig_groq_key


# ===========================================================================
# AIProviderManager - config-driven chain ordering, skip-unconfigured,
# fallback on provider-level failure, NO fallback on an application-level
# one, and the friendly all-failed message. Uses simple Fake provider
# classes swapped into _PROVIDER_CLASSES (same monkeypatch-a-fake-provider
# style as test_agents.py/test_automation.py use for get_ai_provider
# itself), never real network.
# ===========================================================================
import app.ai_provider as ai_provider_module


class FakeProviderBase:
    SUPPORTS_DOCUMENTS = False
    NAME = "fake"

    def __init__(self):
        pass

    def generate_reply(self, messages, **kwargs):
        raise NotImplementedError

    def extract_memories(self, existing, user_message, assistant_reply):
        return {"new": [], "updates": []}

    def upload_document(self, filename, content, mime_type):
        raise ProviderCapabilityError(self.NAME, "upload_document")

    def delete_document(self, file_id):
        raise ProviderCapabilityError(self.NAME, "delete_document")

    def ask_about_documents(self, file_ids, question):
        raise ProviderCapabilityError(self.NAME, "ask_about_documents")

    def learn_skill(self, topic):
        raise NotImplementedError


def make_fake_class(name, behavior, supports_documents=False, not_configured=False):
    """behavior: callable(messages, **kwargs) -> str, or raises."""
    class _Fake(FakeProviderBase):
        NAME = name
        SUPPORTS_DOCUMENTS = supports_documents

        def __init__(self):
            if not_configured:
                raise ProviderNotConfiguredError(name, f"{name} not configured")

        def generate_reply(self, messages, **kwargs):
            return behavior(messages, **kwargs)

        def learn_skill(self, topic):
            return behavior([{"role": "user", "content": topic}])

        def upload_document(self, filename, content, mime_type):
            if supports_documents:
                return f"{name}-file-id"
            raise ProviderCapabilityError(name, "upload_document")

    _Fake.__name__ = f"Fake{name.capitalize()}"
    return _Fake


def with_provider_classes(classes_dict, fn):
    orig = ai_provider_module.AIProviderManager._PROVIDER_CLASSES
    ai_provider_module.AIProviderManager._PROVIDER_CLASSES = classes_dict
    try:
        fn()
    finally:
        ai_provider_module.AIProviderManager._PROVIDER_CLASSES = orig


def with_chain(primary, secondary, tertiary, fn):
    orig = (settings.ai_primary_provider, settings.ai_secondary_provider, settings.ai_tertiary_provider)
    settings.ai_primary_provider, settings.ai_secondary_provider, settings.ai_tertiary_provider = primary, secondary, tertiary
    try:
        fn()
    finally:
        (settings.ai_primary_provider, settings.ai_secondary_provider, settings.ai_tertiary_provider) = orig


# --- chain ordering follows config, in order, deduplicated ---
def _t1():
    classes = {
        "anthropic": make_fake_class("anthropic", lambda m, **k: "a"),
        "gemini": make_fake_class("gemini", lambda m, **k: "g"),
        "groq": make_fake_class("groq", lambda m, **k: "q"),
    }
    def run():
        with_chain("groq", "anthropic", "gemini", lambda: (
            check("AIProviderManager builds its chain in the exact configured order (groq, anthropic, gemini)",
                  [n for n, p in AIProviderManager().providers] == ["groq", "anthropic", "gemini"])
        ))
    with_provider_classes(classes, run)
_t1()


# --- an unconfigured provider is skipped, not fatal ---
def _t2():
    classes = {
        "anthropic": make_fake_class("anthropic", lambda m, **k: "a"),
        "gemini": make_fake_class("gemini", lambda m, **k: "g", not_configured=True),
        "groq": make_fake_class("groq", lambda m, **k: "q"),
    }
    def run():
        mgr = AIProviderManager()
        check("an unconfigured provider (no API key) is left out of the chain, not a crash",
              [n for n, p in mgr.providers] == ["anthropic", "groq"])
        check("the unconfigured provider is recorded in not_configured for diagnostics",
              mgr.not_configured == ["gemini"])
    def run_with_chain():
        with_chain("anthropic", "gemini", "groq", run)
    with_provider_classes(classes, run_with_chain)
_t2()


# --- primary succeeds: no fallback attempted at all ---
def _t3():
    attempted = []
    classes = {
        "anthropic": make_fake_class("anthropic", lambda m, **k: (attempted.append("anthropic"), "ok")[1]),
        "gemini": make_fake_class("gemini", lambda m, **k: (attempted.append("gemini"), "ok")[1]),
    }
    def run():
        with_chain("anthropic", "gemini", "", lambda: [
            check("primary success returns immediately", AIProviderManager().generate_reply([{"role": "user", "content": "hi"}]) == "ok"),
            check("secondary was never even attempted when primary succeeded", attempted == ["anthropic"], attempted),
        ])
    with_provider_classes(classes, run)
_t3()


# --- provider-level failure (ProviderError, fallback-worthy category) falls through to next ---
def _t4():
    attempted = []
    def anthropic_fails(m, **k):
        attempted.append("anthropic")
        raise ProviderError("SERVER_ERROR", "anthropic is down", "anthropic")
    def gemini_ok(m, **k):
        attempted.append("gemini")
        return "gemini answered"
    classes = {
        "anthropic": make_fake_class("anthropic", anthropic_fails),
        "gemini": make_fake_class("gemini", gemini_ok),
    }
    def run():
        with_chain("anthropic", "gemini", "", lambda: [
            check("a SERVER_ERROR from primary falls back to secondary, which answers",
                  AIProviderManager().generate_reply([{"role": "user", "content": "hi"}]) == "gemini answered"),
            check("both providers were actually attempted, in chain order",
                  attempted == ["anthropic", "gemini"], attempted),
        ])
    with_provider_classes(classes, run)
_t4()


# --- application-level error (INVALID_REQUEST) does NOT fall back ---
def _t5():
    attempted = []
    def anthropic_bad_request(m, **k):
        attempted.append("anthropic")
        raise ProviderError("INVALID_REQUEST", "malformed tool schema - our own bug", "anthropic")
    def gemini_ok(m, **k):
        attempted.append("gemini")
        return "gemini answered"
    classes = {
        "anthropic": make_fake_class("anthropic", anthropic_bad_request),
        "gemini": make_fake_class("gemini", gemini_ok),
    }
    def run():
        with_chain("anthropic", "gemini", "", lambda: _check_raises())
    def _check_raises():
        try:
            AIProviderManager().generate_reply([{"role": "user", "content": "hi"}])
            check("an INVALID_REQUEST (our own bug) is NOT swallowed into a fallback - it should raise", False)
        except ProviderError as e:
            check("an INVALID_REQUEST from primary raises immediately instead of falling back", e.category == "INVALID_REQUEST")
        check("the secondary provider was never attempted for a non-fallback-worthy error",
              attempted == ["anthropic"], attempted)
    with_provider_classes(classes, run)
_t5()


# --- unclassified exception (e.g. AnthropicProvider's own raw SDK error) still falls back ---
def _t6():
    attempted = []
    def anthropic_raw_error(m, **k):
        attempted.append("anthropic")
        raise RuntimeError("some raw, unclassified anthropic SDK error")
    def gemini_ok(m, **k):
        attempted.append("gemini")
        return "gemini answered"
    classes = {
        "anthropic": make_fake_class("anthropic", anthropic_raw_error),
        "gemini": make_fake_class("gemini", gemini_ok),
    }
    def run():
        with_chain("anthropic", "gemini", "", lambda: [
            check("an UNCLASSIFIED exception from primary is still treated as fallback-worthy by default",
                  AIProviderManager().generate_reply([{"role": "user", "content": "hi"}]) == "gemini answered"),
            check("both providers were attempted", attempted == ["anthropic", "gemini"], attempted),
        ])
    with_provider_classes(classes, run)
_t6()


# --- every provider fails: friendly message, never a raw exception, for generate_reply ---
def _t7():
    def always_fails(m, **k):
        raise ProviderError("PROVIDER_UNAVAILABLE", "down", "x")
    classes = {
        "anthropic": make_fake_class("anthropic", always_fails),
        "gemini": make_fake_class("gemini", always_fails),
        "groq": make_fake_class("groq", always_fails),
    }
    def run():
        with_chain("anthropic", "gemini", "groq", lambda: check(
            "when every provider fails, generate_reply returns the friendly message (never raises)",
            AIProviderManager().generate_reply([{"role": "user", "content": "hi"}])
            == "I'm temporarily unable to reach my AI providers. Please try again shortly."
        ))
    with_provider_classes(classes, run)
_t7()


# --- no providers configured at all: friendly message, not a crash ---
def _t8():
    classes = {
        "anthropic": make_fake_class("anthropic", lambda m, **k: "a", not_configured=True),
        "gemini": make_fake_class("gemini", lambda m, **k: "g", not_configured=True),
        "groq": make_fake_class("groq", lambda m, **k: "q", not_configured=True),
    }
    def run():
        with_chain("anthropic", "gemini", "groq", lambda: check(
            "with zero configured providers, generate_reply degrades gracefully rather than crashing",
            AIProviderManager().generate_reply([{"role": "user", "content": "hi"}])
            == "I'm temporarily unable to reach my AI providers. Please try again shortly."
        ))
    with_provider_classes(classes, run)
_t8()


# --- document methods route to whichever provider actually supports them, regardless of chain order ---
def _t9():
    classes = {
        "gemini": make_fake_class("gemini", lambda m, **k: "g", supports_documents=False),
        "anthropic": make_fake_class("anthropic", lambda m, **k: "a", supports_documents=True),
    }
    def run():
        # Gemini is PRIMARY here, but doesn't support documents - the call
        # must still land on Anthropic, not fail just because the primary
        # provider can't do it.
        with_chain("gemini", "anthropic", "", lambda: check(
            "upload_document is routed to the provider that supports it, even when it's not primary",
            AIProviderManager().upload_document("f.txt", b"x", "text/plain") == "anthropic-file-id"
        ))
    with_provider_classes(classes, run)
_t9()


def _t10():
    classes = {
        "gemini": make_fake_class("gemini", lambda m, **k: "g", supports_documents=False),
    }
    def run():
        with_chain("gemini", "", "", lambda: _check_raises())
    def _check_raises():
        try:
            AIProviderManager().upload_document("f.txt", b"x", "text/plain")
            check("upload_document raises a clear error when NO configured provider supports documents", False)
        except RuntimeError as e:
            check("upload_document raises a clear error when NO configured provider supports documents",
                  "Knowledge Base" in str(e), str(e))
    with_provider_classes(classes, run)
_t10()


# --- extract_memories: primary only, never raises even if primary's underlying call is broken ---
def _t11():
    classes = {
        "anthropic": make_fake_class("anthropic", lambda m, **k: "a"),
    }
    def run():
        with_chain("anthropic", "", "", lambda: check(
            "extract_memories with a configured provider returns its (best-effort) result, never raises",
            AIProviderManager().extract_memories([], "hi", "hello") == {"new": [], "updates": []}
        ))
    with_provider_classes(classes, run)
_t11()


def _t12():
    classes = {}
    def run():
        with_chain("nonexistent_provider_name", "", "", lambda: [
            check("an unknown provider name in config doesn't crash the manager, just logs an init_error",
                  (mgr := AIProviderManager()).providers == []),
            check("...and generate_reply still degrades gracefully rather than raising",
                  mgr.generate_reply([{"role": "user", "content": "hi"}])
                  == "I'm temporarily unable to reach my AI providers. Please try again shortly."),
        ])
    with_provider_classes(classes, run)
_t12()


print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
