"""
Phase 13 "Debugging Agent" (built 2026-09-20, per Sudeep's explicit choice
of what to build next after Phase 12). Scoped with him first via three
clarifying questions, same as Agent Factory v1 and the Tally self-diagnosis
feature before it: (1) covers BOTH JARVIS's own backend code/errors AND
general code/errors Sudeep hands it directly, (2) diagnose + draft a
proposed fix, but never apply/edit/deploy anything itself - matching the
project charter's hard safety rule ("must never deploy untested code,
modify its own safety controls") and the exact same "diagnose, don't
auto-fix" precedent Sudeep set for the Tally self-diagnosis feature, (3)
starts in chat, not a dedicated dashboard - the same v1-slice pattern every
other custom feature in this project started with.

Two real, permanent capabilities, both read-only:

1. Every unhandled backend exception gets logged here (see main.py's
   app-wide exception handler -> log_backend_exception), the same
   evidence-first pattern already used for Tally failures
   (tally_client.py's log_tally_failure/_log_daybook_query) - so a future
   debugging session (JARVIS itself, or a future Claude session) has real
   evidence to work from instead of a throwaway hand-added debug print.
2. JARVIS can read that log, list its own backend source files, and read a
   file (optionally a line range) from inside a normal chat - see
   ai_provider.py for the three tools this exposes
   (read_recent_backend_errors / list_backend_source_files /
   read_backend_source_file) and the tool-use loop that wires them in.

For the "general code Sudeep gives it" half of the scope, no new mechanism
is needed here - JARVIS already has a chat + sandboxed code-execution loop
(Phase 12, code_executor.py) it can use to actually test a hypothesis
against code/errors Sudeep pastes directly; see JARVIS_SYSTEM_PROMPT in
ai_provider.py for the debugging workflow guidance that ties both halves
together.

Security note (read_backend_source_file/list_backend_source_files): this is
a READ-ONLY tool, strictly scoped to apps/backend (no path traversal
outside it), and deliberately refuses .env/secret-shaped files and common
non-source directories (venv, __pycache__, .git, node_modules) even though
they're technically inside that root - a debugging tool must never be the
thing that leaks a real secret (ANTHROPIC_API_KEY etc.) into a chat
transcript. There is no corresponding write tool anywhere in this module -
on purpose.
"""
import json
import os
import time
import traceback

_APP_DIR = os.path.dirname(os.path.abspath(__file__))          # .../apps/backend/app
_BACKEND_ROOT = os.path.dirname(_APP_DIR)                       # .../apps/backend

BACKEND_ERROR_LOG_PATH = os.path.join(_APP_DIR, "backend_error_log.jsonl")
_TALLY_ERROR_LOG_PATH = os.path.join(_APP_DIR, "tally_error_log.jsonl")

_MAX_LOG_ENTRIES = 200
_MAX_TRACEBACK_CHARS = 4000

# Never let JARVIS read these, even though they're technically under the
# backend root - matches the existing "writes to dotenv files are always
# blocked" rule (see progress-tracker.md's Environment facts) extended to
# reads: an .env file holds real secrets (ANTHROPIC_API_KEY etc.) that must
# never be echoed back into a chat transcript.
_BLOCKED_NAME_SUBSTRINGS = ("secret", "credential", "password", "apikey", "api_key", "api-key")
_BLOCKED_EXTENSIONS = {".key", ".pem", ".pfx", ".p12", ".env"}
_BLOCKED_DIR_NAMES = {"venv", "__pycache__", ".git", "node_modules", ".pytest_cache", ".mypy_cache"}
_ALLOWED_EXTENSIONS = {".py", ".txt", ".md", ".cfg", ".ini", ".toml"}
_MAX_READ_CHARS = 200_000
_MAX_LIST_RESULTS = 500


def log_backend_exception(method: str, path: str, exc: Exception) -> None:
    """Permanently records an unhandled backend exception (see main.py's
    app-wide exception handler). Never raises itself - a logging failure
    must never turn one real bug into a second, worse one (a crashed error
    handler that hides the original exception from the client)."""
    try:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        if len(tb) > _MAX_TRACEBACK_CHARS:
            tb = tb[:_MAX_TRACEBACK_CHARS] + "\n... (truncated)"
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "method": method,
            "path": path,
            "exception_type": type(exc).__name__,
            "message": str(exc)[:2000],
            "traceback": tb,
        }
        _append_capped(BACKEND_ERROR_LOG_PATH, entry)
    except Exception:
        pass


def _append_capped(path: str, entry: dict) -> None:
    """Same capped-rewrite pattern as tally_client.py's log_tally_failure /
    _log_daybook_query: this environment can't always delete files, so the
    log is rewritten with only the most recent _MAX_LOG_ENTRIES rather than
    growing forever."""
    entries = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except Exception:
            entries = []
    entries.append(entry)
    entries = entries[-_MAX_LOG_ENTRIES:]
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _read_log_entries(path: str, source_label: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    entries = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                data = dict(data)
                data["_source"] = source_label
                entries.append(data)
    except Exception:
        return []
    return entries


def read_recent_errors(limit: "int | str" = 10, component: str = "all") -> str:
    """Formats the most recent logged errors as plain text for the model to
    read back to Sudeep. component: "backend" (unhandled exceptions from any
    endpoint, via log_backend_exception above), "tally" (tally_client.py's
    existing log_tally_failure output - the write-path failure log, not the
    read-path query log), or "all" (both, merged and sorted newest-first).
    Never raises - a logging/read hiccup here must never break the chat
    reply that's asking about it."""
    try:
        limit = max(1, min(int(limit or 10), 50))
    except (TypeError, ValueError):
        limit = 10
    component = (component or "all").strip().lower()
    if component not in ("all", "backend", "tally"):
        component = "all"

    entries: list[dict] = []
    if component in ("all", "backend"):
        entries.extend(_read_log_entries(BACKEND_ERROR_LOG_PATH, "backend"))
    if component in ("all", "tally"):
        entries.extend(_read_log_entries(_TALLY_ERROR_LOG_PATH, "tally"))

    if not entries:
        return (
            "No logged errors found for this component - either nothing has "
            "actually failed, or it hasn't been logged yet."
        )

    entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    entries = entries[:limit]

    lines = [f"{len(entries)} most recent logged error(s):"]
    for e in entries:
        src = e.get("_source", "?")
        ts = e.get("timestamp", "?")
        if src == "backend":
            lines.append(
                f"\n[{ts}] (backend) {e.get('method', '?')} {e.get('path', '?')} - "
                f"{e.get('exception_type', '?')}: {e.get('message', '')}\n"
                f"Traceback:\n{e.get('traceback', '(none captured)')}"
            )
        else:
            findings = e.get("findings") or []
            findings_text = (
                "; ".join(f.get("text", "") for f in findings if isinstance(f, dict))
                if findings else "(no findings recorded)"
            )
            lines.append(f"\n[{ts}] (tally) {e.get('message', '')}\nFindings: {findings_text}")
    return "\n".join(lines)


def _resolve_within_backend_root(file_path: str) -> "tuple[str | None, str | None]":
    """Shared path-safety check for read_backend_source/list_backend_files.
    Returns (resolved_absolute_path, None) on success, or (None, error_text)
    when the path escapes apps/backend or falls inside a blocked directory.
    Deliberately conservative - a debugging tool reading outside its own
    project folder, or into dependency/build-artifact directories, is a
    real risk with no debugging upside."""
    root_real = os.path.realpath(_BACKEND_ROOT)
    normalized = (file_path or "").strip().lstrip("/\\")
    candidate = os.path.realpath(os.path.join(root_real, normalized))

    if not (candidate == root_real or candidate.startswith(root_real + os.sep)):
        return None, f"Can't access \"{file_path}\" - it's outside the backend project folder."

    rel_parts = os.path.relpath(candidate, root_real).split(os.sep)
    if any(part in _BLOCKED_DIR_NAMES for part in rel_parts):
        return None, (
            f"Can't access \"{file_path}\" - that's inside a folder JARVIS "
            "doesn't read from (dependencies/build artifacts, not its own code)."
        )
    return candidate, None


def _is_blocked_filename(basename: str) -> bool:
    _, ext = os.path.splitext(basename)
    return (
        basename.startswith(".env")
        or ext.lower() in _BLOCKED_EXTENSIONS
        or any(s in basename.lower() for s in _BLOCKED_NAME_SUBSTRINGS)
    )


def read_backend_source(file_path: str, start_line: "int | None" = None, end_line: "int | None" = None) -> str:
    """Read-only access to JARVIS's own backend Python source, so it can
    actually inspect the real code around a reported error instead of
    reconstructing it from memory of an earlier conversation (backend files
    change over time - see progress-tracker.md's repeated "pull the actual
    current content, don't assume the sandbox/memory is current" lesson).
    file_path is relative to apps/backend, e.g. "app/tally_client.py" or
    "app/routers/chat_routes.py". start_line/end_line optionally narrow the
    read to a range (1-indexed, inclusive) - useful once a traceback names a
    specific line. Never raises."""
    if not file_path or not isinstance(file_path, str):
        return "No file path given."

    candidate, error = _resolve_within_backend_root(file_path)
    if error:
        return error

    basename = os.path.basename(candidate)
    if _is_blocked_filename(basename):
        return (
            f"Can't read \"{file_path}\" - JARVIS never reads files that "
            "might hold secrets or credentials, even its own."
        )

    _, ext = os.path.splitext(basename)
    if ext.lower() not in _ALLOWED_EXTENSIONS:
        return (
            f"Can't read \"{file_path}\" - only backend source/text files "
            f"({', '.join(sorted(_ALLOWED_EXTENSIONS))}) are readable, not this file type."
        )

    if not os.path.isfile(candidate):
        return f"\"{file_path}\" doesn't exist in the backend project folder."

    try:
        with open(candidate, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        return f"Couldn't read \"{file_path}\": {e}"

    lines = content.splitlines()
    total_lines = len(lines)
    try:
        s = max(1, int(start_line)) if start_line else 1
    except (TypeError, ValueError):
        s = 1
    try:
        e_ = min(total_lines, int(end_line)) if end_line else total_lines
    except (TypeError, ValueError):
        e_ = total_lines
    if total_lines == 0:
        return f"{file_path} is empty."
    if s > total_lines:
        return f"\"{file_path}\" only has {total_lines} lines - line {s} doesn't exist."
    if e_ < s:
        e_ = s

    selected = lines[s - 1:e_]
    numbered = "\n".join(f"{i}: {line}" for i, line in enumerate(selected, start=s))
    if len(numbered) > _MAX_READ_CHARS:
        numbered = numbered[:_MAX_READ_CHARS] + "\n... (truncated - ask for a narrower line range)"

    return f"{file_path} (lines {s}-{e_} of {total_lines}):\n{numbered}"


def list_backend_source_files(subdirectory: str = "") -> str:
    """Lists readable backend source files (same allowlist/blocklist as
    read_backend_source) so the model can find the right file instead of
    guessing an exact path. Optionally scoped to a subdirectory (e.g.
    "app/routers"). Never raises."""
    candidate, error = _resolve_within_backend_root(subdirectory or "")
    if error:
        return error
    if not os.path.isdir(candidate):
        return f"\"{subdirectory}\" isn't a folder in the backend project."

    root_real = os.path.realpath(_BACKEND_ROOT)
    results = []
    try:
        for dirpath, dirnames, filenames in os.walk(candidate):
            dirnames[:] = [
                d for d in dirnames if d not in _BLOCKED_DIR_NAMES and not d.startswith(".")
            ]
            for fn in filenames:
                _, ext = os.path.splitext(fn)
                if ext.lower() not in _ALLOWED_EXTENSIONS or _is_blocked_filename(fn):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn), root_real)
                results.append(rel.replace(os.sep, "/"))
                if len(results) >= _MAX_LIST_RESULTS:
                    break
            if len(results) >= _MAX_LIST_RESULTS:
                break
    except Exception as e:
        return f"Couldn't list \"{subdirectory}\": {e}"

    results.sort()
    if not results:
        return f"No readable source files found under \"{subdirectory or '.'}\"."
    suffix = " (more exist, narrow to a subdirectory)" if len(results) >= _MAX_LIST_RESULTS else ""
    return f"{len(results)} readable file(s) under \"{subdirectory or '.'}\"{suffix}:\n" + "\n".join(results)
