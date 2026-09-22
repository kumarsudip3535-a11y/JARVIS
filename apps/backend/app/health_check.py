"""
Phase 14 "Self-diagnostics" and Phase 15 "Self-healing" (built 2026-09-20,
per Sudeep's explicit choice of what to build next after Phase 13). Scoped
with him first via three clarifying questions, same pattern as every other
open-ended custom feature in this project:

1. **What should the health check cover?** -> Core backend + every
   integration JARVIS actually depends on today: the AI provider, the
   database, the Tally connection, the Docker sandbox (Phase 12), and web
   search.
2. **How should it run?** -> On-demand only via chat for v1 ("run a health
   check") - not periodic/background checks, since there's nowhere for a
   background check to proactively surface a problem to yet (Proactive
   mode, charter Phase 24, isn't built).
3. **What does "self-healing" mean, given the hard safety rule that JARVIS
   must never deploy untested code or modify its own safety controls?** ->
   Deliberately narrow: **safe automatic recovery only**. JARVIS may retry a
   genuinely transient failure (a brief connection blip) a couple of times
   before giving up - it never touches code, never changes a setting, and
   a real problem still gets reported plainly rather than silently retried
   forever. This module implements the retry/backoff logic for each
   subsystem check below; the equivalent retry for the REAL Tally send path
   (not just this diagnostic) lives in tally_client.py's send_to_tally -
   see its own docstring note for why that's the safe place for it.

This module is read-only diagnostics - it never writes to Sudeep's Tally,
never touches the database beyond a trivial SELECT 1, and never changes any
setting. See ai_provider.py for the run_system_health_check tool this
exposes, wired into the same client-side tool-use loop as the Tally and
Phase 13 debugging tools.
"""
import time

from sqlalchemy import text

from app.config import settings
from app.database import engine
from app import tally_client
from app.code_executor import CodeExecutor

# Phase 15 "Self-healing" scope for THIS module's checks (database, Tally
# reachability) - a couple of quick retries for a check that looks
# transient, then report it plainly. Kept small/short since a health check
# should still feel instant, not itself hang for a long time investigating
# a real outage.
_RETRY_ATTEMPTS = 2
_RETRY_DELAY_SECONDS = 1.5

_STATUS_LABELS = {
    "ai_provider": "AI provider",
    "database": "Database",
    "tally": "Tally accounting connection",
    "docker_code_execution": "Docker (code execution sandbox, Phase 12)",
    "web_search": "Web search",
}
_STATUS_ORDER = {"ERROR": 0, "WARNING": 1, "OFFLINE": 2, "ONLINE": 3}


def _retry_suffix(attempt: int) -> str:
    if not attempt:
        return ""
    return f" (recovered after {attempt} retr{'y' if attempt == 1 else 'ies'})"


_PROVIDER_KEY_ATTR = {
    "anthropic": "anthropic_api_key",
    "gemini": "gemini_api_key",
    "groq": "groq_api_key",
}
_PROVIDER_MODEL_ATTR = {
    "anthropic": "anthropic_model",
    "gemini": "gemini_model",
    "groq": "groq_model",
}
_PROVIDER_DISPLAY_NAME = {
    "anthropic": "Anthropic (Claude)",
    "gemini": "Google Gemini",
    "groq": "Groq",
}


def _check_ai_provider() -> dict:
    """Deliberately NOT a fresh, billed API call - see the module docstring.
    This reply is itself proof the ACTIVE provider (whichever one in the
    chain below actually answered) is reachable and working (you're reading
    it), so a second probe call here would just spend real money/tokens to
    confirm something already demonstrated by the fact this code is running
    at all. Reports configuration correctness for the whole fallback chain
    instead.

    Fixed 2026-09-21 (Multi-Model AI Brain upgrade, same day it shipped):
    this used to check the OLD, pre-upgrade `settings.ai_provider` /
    `AI_PROVIDER` field, which `AIProviderManager` itself no longer reads
    at all (kept only for backward-compat - see `get_ai_provider()`'s
    docstring). That meant this health check could keep reporting
    "Configured for Anthropic" even after `AI_PRIMARY_PROVIDER` was
    switched to something else entirely - a real, previously-shipped bug
    in this same upgrade, caught and fixed before it could mislead the
    first live Gemini/Groq test. Now reports the REAL configured chain
    (`AI_PRIMARY_PROVIDER` / `AI_SECONDARY_PROVIDER` / `AI_TERTIARY_
    PROVIDER`), built and deduped the exact same way `AIProviderManager.
    __init__` itself does, so this check can never again drift from what
    the app is actually using."""
    chain = []
    seen = set()
    for raw_name in (settings.ai_primary_provider, settings.ai_secondary_provider, settings.ai_tertiary_provider):
        name = (raw_name or "").strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        chain.append(name)

    if not chain:
        return {"status": "ERROR", "detail": "No AI provider chain configured at all (AI_PRIMARY_PROVIDER is empty)."}

    configured = []
    unconfigured = []
    unknown = []
    for name in chain:
        if name not in _PROVIDER_KEY_ATTR:
            unknown.append(name)
            continue
        if getattr(settings, _PROVIDER_KEY_ATTR[name], ""):
            model = getattr(settings, _PROVIDER_MODEL_ATTR[name], "")
            configured.append(f'{_PROVIDER_DISPLAY_NAME.get(name, name)} (model "{model}")')
        else:
            unconfigured.append(_PROVIDER_DISPLAY_NAME.get(name, name))

    if unknown:
        return {
            "status": "ERROR",
            "detail": (
                f"Unrecognized provider name(s) in the configured chain: {', '.join(unknown)}. "
                "Valid values are anthropic, gemini, groq."
            ),
        }
    if not configured:
        return {
            "status": "ERROR",
            "detail": (
                f"No provider in the configured chain ({' -> '.join(chain)}) has an API key set "
                "in the backend's .env - every request would fail."
            ),
        }

    detail = (
        f"Active/primary: {configured[0]} - confirmed reachable by the fact "
        "that this very check is running as a reply generated by it."
    )
    if len(configured) > 1:
        detail += f" Fallback also configured: {', '.join(configured[1:])}."
    if unconfigured:
        detail += f" Not yet configured (no key set, skipped): {', '.join(unconfigured)}."
    return {"status": "ONLINE", "detail": detail}


def _check_database() -> dict:
    """A real reachability check - actually runs a trivial query, not just
    'is a URL configured'. Retried (Phase 15) since a brief DB blip (e.g.
    Postgres still starting up after a PC restart) is a genuinely transient
    failure, safe to retry automatically."""
    last_error = None
    for attempt in range(_RETRY_ATTEMPTS + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return {"status": "ONLINE", "detail": "Database reachable." + _retry_suffix(attempt)}
        except Exception as e:
            last_error = e
            if attempt < _RETRY_ATTEMPTS:
                time.sleep(_RETRY_DELAY_SECONDS)
    return {"status": "ERROR", "detail": f"Database unreachable: {last_error}"}


def _check_tally() -> dict:
    if not settings.tally_enabled:
        return {"status": "OFFLINE", "detail": "Tally integration is turned off (TALLY_ENABLED=false)."}
    if not settings.tally_company_name:
        return {"status": "WARNING", "detail": "Tally is enabled but TALLY_COMPANY_NAME isn't set yet."}

    last_message = None
    for attempt in range(_RETRY_ATTEMPTS + 1):
        reachable, message = tally_client.check_tally_reachable(
            settings.tally_host, settings.tally_port, settings.tally_timeout_seconds
        )
        if reachable:
            return {"status": "ONLINE", "detail": message + _retry_suffix(attempt)}
        last_message = message
        if attempt < _RETRY_ATTEMPTS:
            time.sleep(_RETRY_DELAY_SECONDS)
    return {"status": "ERROR", "detail": last_message}


def _check_docker() -> dict:
    """Reuses Phase 12's own readiness check (code_executor.py), forcing a
    FRESH check (force=True) rather than trusting whatever was cached from
    the last code execution - a health check must never report a stale
    answer just because Docker happened to be down the first time it was
    checked in this backend process's lifetime."""
    ready, reason = CodeExecutor._docker_ready(force=True)
    if ready:
        return {
            "status": "ONLINE",
            "detail": "Docker is running - Python/JavaScript code execution (Phase 12) is available.",
        }
    return {
        "status": "WARNING",
        "detail": f"Docker isn't ready - code execution will be refused until it is. {reason}",
    }


def _check_web_search() -> dict:
    if not settings.web_search_enabled:
        return {"status": "OFFLINE", "detail": "Web search is turned off (WEB_SEARCH_ENABLED=false)."}
    return {
        "status": "ONLINE",
        "detail": f"Web search is enabled (up to {settings.web_search_max_uses} searches per reply).",
    }


def run_health_check() -> dict:
    """Runs every subsystem check and returns {"name": {"status", "detail"}}.
    Never raises - a single subsystem's check function raising unexpectedly
    is itself caught and reported as that subsystem's own ERROR, so one
    broken check can't hide the results of every other one."""
    checks = {
        "ai_provider": _check_ai_provider,
        "database": _check_database,
        "tally": _check_tally,
        "docker_code_execution": _check_docker,
        "web_search": _check_web_search,
    }
    results = {}
    for name, fn in checks.items():
        try:
            results[name] = fn()
        except Exception as e:
            results[name] = {"status": "ERROR", "detail": f"This check itself failed to run: {e}"}
    return results


def format_health_check(results: dict) -> str:
    """Formats run_health_check()'s output as plain text for the model to
    relay - sorted worst-first (ERROR, then WARNING, then OFFLINE, then
    ONLINE) so a real problem is never buried below a long list of healthy
    subsystems."""
    ordered = sorted(
        results.items(),
        key=lambda kv: _STATUS_ORDER.get((kv[1] or {}).get("status"), 1),
    )
    any_problem = any((r or {}).get("status") in ("ERROR", "WARNING") for r in results.values())
    lines = ["PROBLEM(S) FOUND:" if any_problem else "ALL SYSTEMS OK:"]
    for name, result in ordered:
        label = _STATUS_LABELS.get(name, name)
        status = (result or {}).get("status", "?")
        detail = (result or {}).get("detail", "")
        lines.append(f"- [{status}] {label}: {detail}")
    return "\n".join(lines)


def run_and_format_health_check() -> str:
    """Convenience wrapper - the actual callable wired into
    ai_provider.py's tool-use loop (see chat_routes.py). Never raises."""
    try:
        return format_health_check(run_health_check())
    except Exception as e:
        return f"Couldn't complete the health check: {e}"
