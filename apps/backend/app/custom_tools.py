"""Phase 16 "Tool/plugin architecture" (built 2026-09-20, scoped with Sudeep
via three clarifying questions - see progress-tracker.md). Lets Sudeep
define his own simple HTTP-based tools (a name, a description that tells
JARVIS what it's for, a URL, a method, named parameters, and an optional
API key) that any Agent can be assigned - see the CustomTool model in
models.py and tool_routes.py for the CRUD API. This is the "real tool
catalog" half of Phase 16; ai_provider.py's tool-use loop is where an
assigned custom tool actually gets offered to and called by the model.

Two safety choices, both deliberate and documented (see config.py):

1. Any API key/secret a tool needs is encrypted at rest with a Fernet key
   (settings.tool_secrets_key, generated once by Sudeep - see config.py's
   comment) rather than stored in plain text in the database - the same
   "never store a secret where it could leak" caution already applied to
   .env files elsewhere in this project (debug_agent.py's blocked-filename
   list), extended to this new kind of secret.
2. A read-only (GET) tool is actually called live. A non-read-only one
   (POST/PUT/PATCH/DELETE) is NEVER actually sent in v1 - invoke_custom_tool
   builds and returns a clearly-labeled DRAFT of exactly what it would send
   instead. This is the same "never take an autonomous action with a real
   side effect without Sudeep in the loop" caution behind the Tally
   review-before-send flow, without building a second full review-and-
   confirm HTTP round trip for every possible external action in v1 - a
   real, one-click "send" for action calls is a natural v2 addition once
   there's an actual use case that needs it.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models import CustomTool

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_CALL_LOG_PATH = os.path.join(_APP_DIR, "custom_tool_call_log.jsonl")
_MAX_LOG_ENTRIES = 200

ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_ALLOWED_SCHEMES = {"http", "https"}


class CustomToolError(Exception):
    """Raised for a genuine configuration problem (bad URL/method, etc.) -
    never for a normal network failure, which invoke_custom_tool always
    turns into a plain-text result instead so a flaky external API can't
    break the whole chat reply."""


def _get_fernet():
    """Returns a Fernet instance built from settings.tool_secrets_key, or
    None if it isn't set / the cryptography package isn't installed - every
    caller here treats None as "secret storage isn't available yet" rather
    than raising, so the rest of the tool registry (read-only tools that
    don't need a secret) keeps working either way."""
    if not settings.tool_secrets_key:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(settings.tool_secrets_key.encode("utf-8"))
    except Exception:
        return None


def encrypt_secret(plain_value: str) -> str:
    """Raises CustomToolError if TOOL_SECRETS_KEY isn't configured (or is
    invalid) - a tool needing a secret simply can't be created until that's
    set up, rather than ever falling back to storing it in plain text."""
    fernet = _get_fernet()
    if fernet is None:
        raise CustomToolError(
            "TOOL_SECRETS_KEY isn't set (or isn't valid) in .env, so a secret "
            "can't be stored securely yet - see config.py for how to generate "
            "one. Tools that don't need a secret still work fine without it."
        )
    return fernet.encrypt(plain_value.encode("utf-8")).decode("utf-8")


def decrypt_secret(encrypted_value: str) -> str | None:
    """Never raises - a decryption failure (key rotated, corrupted value,
    library missing) just means this call can't be authenticated, handled
    as a normal invoke_custom_tool failure rather than a crash."""
    fernet = _get_fernet()
    if fernet is None:
        return None
    try:
        return fernet.decrypt(encrypted_value.encode("utf-8")).decode("utf-8")
    except Exception:
        return None


def _append_capped(entry: dict) -> None:
    """Same capped-rewrite log pattern as debug_agent.py/tally_client.py -
    this environment can't always delete files, so the log is rewritten
    with only the most recent _MAX_LOG_ENTRIES rather than growing forever.
    Never raises."""
    try:
        entries = []
        if os.path.exists(_CALL_LOG_PATH):
            with open(_CALL_LOG_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        entries.append(entry)
        entries = entries[-_MAX_LOG_ENTRIES:]
        with open(_CALL_LOG_PATH, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
    except Exception:
        pass


def build_tool_schema(tool: CustomTool) -> dict:
    """Builds the Anthropic tool schema for one custom tool, the same shape
    as the hand-written *_TOOL dicts in ai_provider.py - the difference here
    is these are built dynamically, one per assigned CustomTool row, rather
    than fixed at import time."""
    try:
        params = json.loads(tool.param_schema or "[]")
    except (json.JSONDecodeError, TypeError):
        params = []

    properties = {}
    required = []
    for p in params:
        pname = p.get("name")
        if not pname:
            continue
        properties[pname] = {
            "type": "string",
            "description": p.get("description") or "",
        }
        if p.get("required"):
            required.append(pname)

    description = tool.description
    if not tool.is_read_only:
        description = (
            f"{description}\n\n(This is a {tool.http_method} action, not a read - "
            "calling it only prepares a draft of exactly what would be sent; "
            "it is never actually sent automatically. Show the draft to Sudeep "
            "and tell him he'd need to send it himself.)"
        )

    return {
        "name": f"custom_tool_{tool.id}",
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


def _redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "[REDACTED]")
    return text


def invoke_custom_tool(tool: CustomTool, params: dict[str, Any]) -> str:
    """Actually runs a read-only (GET) custom tool live, or builds a
    never-sent draft for anything else - see the module docstring for why.
    Never raises: every real failure mode (bad URL, network error, timeout,
    oversized response) is turned into a clear, specific text result so the
    calling model always gets something useful to relay, the same "never let
    a tool failure break the whole reply" pattern used by every other
    client-side tool in this project (see ai_provider.py's tool-use loop)."""
    try:
        parsed = urllib.parse.urlparse(tool.url)
    except Exception:
        return f"This tool's URL (\"{tool.url}\") isn't valid - ask Sudeep to fix it on the Tools page."
    if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.netloc:
        return (
            f"This tool's URL (\"{tool.url}\") isn't a valid http/https address - "
            "ask Sudeep to fix it on the Tools page."
        )

    method = (tool.http_method or "GET").upper()
    if method not in ALLOWED_METHODS:
        return f"This tool is configured with an unsupported method (\"{tool.http_method}\")."

    try:
        static_headers = json.loads(tool.static_headers or "{}")
        if not isinstance(static_headers, dict):
            static_headers = {}
    except (json.JSONDecodeError, TypeError):
        static_headers = {}

    headers = dict(static_headers)
    secret_value = None
    if tool.auth_header_name and tool.encrypted_auth_value:
        secret_value = decrypt_secret(tool.encrypted_auth_value)
        if secret_value is None:
            return (
                "This tool needs an API key to call, but it couldn't be decrypted "
                "(TOOL_SECRETS_KEY may be missing, changed, or not set in .env) - "
                "ask Sudeep to re-enter the key on the Tools page."
            )
        headers[tool.auth_header_name] = secret_value

    safe_params = {k: ("" if v is None else str(v)) for k, v in (params or {}).items()}

    if not tool.is_read_only:
        # DRAFT ONLY - see the module docstring. Never actually sends the
        # request, whatever the method is.
        lines = [
            f"DRAFT (not sent - {method} calls are drafted-only in v1, see the tool's description):",
            f"  {method} {tool.url}",
        ]
        if headers:
            shown_headers = dict(headers)
            if tool.auth_header_name and tool.auth_header_name in shown_headers:
                shown_headers[tool.auth_header_name] = "[the configured API key]"
            lines.append(f"  Headers: {json.dumps(shown_headers)}")
        if safe_params:
            lines.append(f"  Body/params: {json.dumps(safe_params)}")
        lines.append(
            "Tell Sudeep this is a draft only - JARVIS never sends an action "
            "call like this automatically; he'd need to send it himself."
        )
        return "\n".join(lines)

    url = tool.url
    if safe_params:
        separator = "&" if urllib.parse.urlparse(url).query else "?"
        url = f"{url}{separator}{urllib.parse.urlencode(safe_params)}"

    request = urllib.request.Request(url, headers=headers, method="GET")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=settings.tool_call_timeout_seconds) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as e:
        raw = None
        status = e.code
        result_text = f"The tool returned an error: HTTP {e.code} {e.reason}"
        _log_call(tool, method, status, ok=False, secrets=[secret_value])
        return _redact(result_text, [secret_value] if secret_value else [])
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        _log_call(tool, method, None, ok=False, secrets=[secret_value])
        return _redact(f"Couldn't reach this tool's URL: {e}", [secret_value] if secret_value else [])
    except Exception as e:
        _log_call(tool, method, None, ok=False, secrets=[secret_value])
        return _redact(f"This tool call failed: {e}", [secret_value] if secret_value else [])

    elapsed = time.monotonic() - started
    _log_call(tool, method, status, ok=True, secrets=[secret_value], elapsed=elapsed)

    if len(raw) > settings.tool_max_response_chars:
        raw = raw[: settings.tool_max_response_chars] + "\n... (truncated)"
    return _redact(raw, [secret_value] if secret_value else [])


def _log_call(tool: CustomTool, method: str, status: int | None, ok: bool, secrets: list[str | None], elapsed: float | None = None) -> None:
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tool_id": tool.id,
        "tool_name": tool.name,
        "method": method,
        "status": status,
        "ok": ok,
        "elapsed_seconds": round(elapsed, 3) if elapsed is not None else None,
    }
    _append_capped(entry)


def get_agent_custom_tools(db: Session, user_id: int, assigned_ids: list[int]) -> list[CustomTool]:
    """Loads only the enabled, still-existing CustomTool rows an agent is
    assigned, in the order they were assigned - a deleted or disabled tool
    just silently drops out rather than erroring, the same tolerant pattern
    used for a stale assigned_skill_id."""
    if not assigned_ids:
        return []
    rows = (
        db.query(CustomTool)
        .filter(
            CustomTool.user_id == user_id,
            CustomTool.id.in_(assigned_ids),
            CustomTool.enabled.is_(True),
        )
        .all()
    )
    by_id = {t.id: t for t in rows}
    return [by_id[tid] for tid in assigned_ids if tid in by_id]
