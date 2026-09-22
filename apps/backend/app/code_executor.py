"""
Safe code execution sandbox for Python, JavaScript, and SQL.

SECURITY MODEL REWRITTEN 2026-09-20 (Phase 12 "Coding Agent" audit - the
first time any session actually reviewed this feature since it was
discovered already existing on Sudeep's machine, undocumented, on
2026-09-19). See progress-tracker.md for the full write-up. Short version:

The ORIGINAL implementation ran the real system `python`/`node` interpreter
directly via subprocess.run(["python", temp_file]) - i.e. Sudeep's actual
machine, with his actual user's real file/network access - and tried to
stop dangerous code with nothing but a case-lowered SUBSTRING blocklist on
the source text (checking for "import os", "require('fs')", etc.). This is
NOT a real sandbox and was trivially bypassable, e.g.:
  - `__import__('os').system('...')` never contains the string "import os",
    so it sailed straight past the Python blocklist into a real OS command.
  - `importlib.import_module('os')` - same bypass, different spelling.
  - `require("fs")` (double quotes) or `require ('fs')` (a space) never
    matched the single-quoted, no-space `require('fs')` pattern checked in
    JavaScript.
  - `eval`/`exec`/`compile`/`open` were listed as "blocked" but the check
    only looked for the nonsensical patterns "import eval"/"from eval"
    etc. - the actual dangerous calls (`eval(...)`, `open(...)`) were never
    checked at all.
A blocklist can never be made airtight against a Turing-complete language
(there are always more ways to reach the same dangerous call), so the fix
here is real OS-level isolation, not a better blocklist - Python and
JavaScript now each run inside a fresh, disposable Docker container:
  - --network none (no network access at all from inside the container)
  - --memory/--memory-swap/--cpus/--pids-limit (hard resource ceilings, so
    runaway or bomb-y code can't affect the host)
  - --read-only root filesystem + a small writable /tmp tmpfs (nothing the
    code does can persist or reach outside the container)
  - --cap-drop ALL --security-opt no-new-privileges (drops Linux
    capabilities and blocks privilege-escalation via setuid binaries)
  - --rm (the container is destroyed the moment it exits - never reused,
    never left running)
This DOES require Docker Desktop to actually be running on Sudeep's
machine - already a hard requirement for the rest of this project (local
Postgres/Redis run the same way), so this isn't a new dependency, just
another consumer of one that was already required. If Docker isn't
reachable, execution is refused with a clear error - it deliberately does
NOT fall back to the old direct-subprocess path, since that reintroduces
exactly the hole this rewrite closes.

SQL is UNCHANGED - an in-memory SQLite database (cursor.executescript
against :memory:, no file access, no extension loading enabled) was never
actually exploitable the way the Python/JS paths were; there's no
exec/xp_cmdshell equivalent in standard SQLite. No sandboxing gap there.

NOT YET LIVE-TESTED against Sudeep's actual Windows + Docker Desktop setup
(this environment can't reach his real Docker daemon) - see
progress-tracker.md for what's been verified (command construction, error
handling, timeout/cleanup logic, all via mocked subprocess calls) versus
what still needs a real live run to confirm (in particular, whether the
Windows-host temp file bind-mounts cleanly into the Linux container - this
is standard, well-supported Docker Desktop behavior, but hasn't been
proven against Sudeep's own machine yet).
"""
import shutil
import subprocess
import tempfile
import time
import uuid
import sqlite3
from pathlib import Path


class CodeExecutionError(Exception):
    """Raised when code execution fails."""
    pass


class CodeExecutor:
    """Handles safe execution of user code in isolated Docker containers
    (Python/JavaScript) or an in-memory SQLite database (SQL)."""

    PYTHON_TIMEOUT = 30  # seconds - the running-the-user's-code budget
    JS_TIMEOUT = 30
    SQL_TIMEOUT = 10
    DOCKER_READY_CHECK_TIMEOUT = 10
    IMAGE_PULL_TIMEOUT = 300  # first use of an image only - see _ensure_image

    PYTHON_IMAGE = "python:3.12-slim"
    JS_IMAGE = "node:20-slim"

    # Cached per-process so every single execution doesn't re-shell-out to
    # `docker info` - reset only by restarting the backend. A transient
    # Docker outage that starts mid-session won't be re-detected until
    # restart, which is an acceptable trade-off for how often this is
    # actually called; revisit if that turns out to matter in practice.
    _docker_ready_cache: "tuple[bool, str | None] | None" = None
    _pulled_images: "set[str]" = set()

    @staticmethod
    def _docker_ready(force: bool = False) -> "tuple[bool, str | None]":
        """(True, None) once the docker CLI exists AND the daemon actually
        answers `docker info`. Cached after the first check (a real, if
        rare, staleness gap: if Docker Desktop is started AFTER a failed
        check earlier in this backend process's lifetime, the cached
        "not ready" would otherwise persist until a restart). force=True
        (added 2026-09-20 for Phase 14 "Self-diagnostics" - see
        health_check.py) bypasses and refreshes the cache, since a health
        check must never report a stale answer; normal code-execution calls
        keep using the cache (force=False, the default) to avoid a
        subprocess call on every single run."""
        if CodeExecutor._docker_ready_cache is not None and not force:
            return CodeExecutor._docker_ready_cache
        if shutil.which("docker") is None:
            result = (False, "Docker isn't installed, or isn't on PATH.")
        else:
            try:
                proc = subprocess.run(
                    ["docker", "info"],
                    capture_output=True,
                    text=True,
                    timeout=CodeExecutor.DOCKER_READY_CHECK_TIMEOUT,
                )
                if proc.returncode == 0:
                    result = (True, None)
                else:
                    result = (
                        False,
                        "Docker Desktop doesn't seem to be running (the Docker daemon didn't respond) - "
                        "start Docker Desktop and try again.",
                    )
            except subprocess.TimeoutExpired:
                result = (False, "Docker didn't respond in time - Docker Desktop may still be starting up.")
            except OSError as e:
                result = (False, f"Couldn't reach Docker: {e}")
        CodeExecutor._docker_ready_cache = result
        return result

    @staticmethod
    def _ensure_image(image: str) -> "tuple[bool, str | None]":
        """Makes sure `image` is already pulled, pulling it (with a much
        longer timeout, since a fresh pull can genuinely take a while) the
        first time it's needed. Cached afterwards so ordinary executions
        stay fast - only the very first Python or JavaScript run on a
        freshly-set-up machine pays the pull cost."""
        if image in CodeExecutor._pulled_images:
            return True, None
        try:
            inspect = subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True,
                text=True,
                timeout=CodeExecutor.DOCKER_READY_CHECK_TIMEOUT,
            )
            if inspect.returncode == 0:
                CodeExecutor._pulled_images.add(image)
                return True, None
            pull = subprocess.run(
                ["docker", "pull", image],
                capture_output=True,
                text=True,
                timeout=CodeExecutor.IMAGE_PULL_TIMEOUT,
            )
            if pull.returncode == 0:
                CodeExecutor._pulled_images.add(image)
                return True, None
            return False, f"Couldn't download the sandbox image ({image}): {(pull.stderr or pull.stdout or '').strip()}"
        except subprocess.TimeoutExpired:
            return False, f"Timed out preparing the sandbox image ({image}) - check your internet connection and try again."
        except OSError as e:
            return False, f"Couldn't prepare the sandbox image ({image}): {e}"

    @staticmethod
    def _run_in_docker(
        image: str, host_file: str, container_cmd: "list[str]", timeout: int
    ) -> "tuple[bool, str | None, str | None, float]":
        """Runs container_cmd inside a fresh, disposable, locked-down
        container with host_file bind-mounted read-only at /code/script.
        See the module docstring for exactly what's locked down and why.
        Returns (success, output, error, execution_time) - the same shape
        as before this rewrite, so callers (execute_code, code_routes.py)
        didn't need to change at all."""
        ready, reason = CodeExecutor._docker_ready()
        if not ready:
            return (False, None, f"Sandboxed execution isn't available right now: {reason}", 0.0)

        image_ok, image_reason = CodeExecutor._ensure_image(image)
        if not image_ok:
            return (False, None, image_reason, 0.0)

        container_name = f"jarvis-exec-{uuid.uuid4().hex[:12]}"
        docker_cmd = [
            "docker", "run",
            "--rm",
            "--name", container_name,
            "--network", "none",
            "--memory", "128m",
            "--memory-swap", "128m",
            "--cpus", "0.5",
            "--pids-limit", "64",
            "--read-only",
            "--tmpfs", "/tmp:size=16m",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "-v", f"{host_file}:/code/script:ro",
            image,
        ] + container_cmd

        try:
            start_time = time.time()
            result = subprocess.run(
                docker_cmd, capture_output=True, text=True, timeout=timeout
            )
            execution_time = time.time() - start_time
            if result.returncode == 0:
                return (True, result.stdout.strip() or "(no output)", None, execution_time)
            # A non-zero exit is either the user's own code failing
            # (normal - e.g. a Python traceback) or docker itself failing
            # to start the container - stderr is the most useful signal
            # either way, so surface it as-is rather than trying to tell
            # the two cases apart.
            error_text = (result.stderr or result.stdout or "").strip() or f"Exited with status {result.returncode}"
            return (False, None, error_text, execution_time)
        except subprocess.TimeoutExpired:
            # `docker run` in the foreground does NOT reliably stop the
            # container just because the CLI process waiting on it was
            # killed (a well-known Docker gotcha) - explicitly kill it by
            # name so a timed-out execution never keeps running in the
            # background. --rm means `docker kill` also removes it.
            try:
                subprocess.run(
                    ["docker", "kill", container_name],
                    capture_output=True,
                    timeout=CodeExecutor.DOCKER_READY_CHECK_TIMEOUT,
                )
            except (subprocess.TimeoutExpired, OSError):
                pass
            return (False, None, f"Execution timed out after {timeout} seconds", 0.0)
        except OSError as e:
            return (False, None, f"Execution failed: {e}", 0.0)

    @staticmethod
    def execute_python(code: str) -> "tuple[bool, str | None, str | None, float]":
        """Runs Python code inside a fresh python:3.12-slim container - see
        the module docstring for the full security rationale."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            temp_file = f.name
        try:
            return CodeExecutor._run_in_docker(
                CodeExecutor.PYTHON_IMAGE, temp_file, ["python", "/code/script"], CodeExecutor.PYTHON_TIMEOUT
            )
        finally:
            try:
                Path(temp_file).unlink()
            except OSError:
                pass

    @staticmethod
    def execute_javascript(code: str) -> "tuple[bool, str | None, str | None, float]":
        """Runs JavaScript code inside a fresh node:20-slim container - see
        the module docstring for the full security rationale."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False) as f:
            f.write(code)
            temp_file = f.name
        try:
            return CodeExecutor._run_in_docker(
                CodeExecutor.JS_IMAGE, temp_file, ["node", "/code/script"], CodeExecutor.JS_TIMEOUT
            )
        finally:
            try:
                Path(temp_file).unlink()
            except OSError:
                pass

    @staticmethod
    def execute_sql(code: str) -> "tuple[bool, str | None, str | None, float]":
        """Security model unchanged from before this rewrite - see the
        module docstring for why an in-memory SQLite database was never
        actually exploitable the way the Python/JS paths were.

        FUNCTIONAL BUG FOUND AND FIXED 2026-09-20 (same Phase 12 audit that
        replaced the Python/JS sandboxing): the original implementation ran
        the whole script through `cursor.executescript()`, which Python's
        sqlite3 module never leaves in a state where `cursor.description`/
        `cursor.fetchall()` returns the last statement's rows - so EVERY
        query, including a plain `SELECT 1`, silently came back as "Query
        executed successfully. Rows affected: -1" instead of showing any
        actual data. Confirmed directly: `execute_sql("SELECT 1 AS one;")`
        returned no rows at all before this fix. Not a security issue (see
        module docstring), but the feature was quietly useless for its
        actual purpose - a query sandbox that never shows query results.

        Fixed by splitting the script into individual statements (respecting
        quoted strings, so a semicolon inside a string literal doesn't
        wrongly end a statement) and running all but the last one via plain
        `cursor.execute()`, then running the last one separately so its
        `cursor.description`/`fetchall()` are captured correctly if it's a
        SELECT (or any other row-returning statement). Statement splitting
        is a simple quote-aware scan, not a full SQL parser - a semicolon
        inside a `-- comment` or `/* block comment */` isn't specially
        handled, an acceptable limitation for a sandbox scratch tool.

        Execute SQL code against an in-memory SQLite database.
        Completely isolated - no file system access.

        Returns: (success, output, error, execution_time)
        """
        statements = CodeExecutor._split_sql_statements(code)
        if not statements:
            return (True, "(no statements to execute)", None, 0.0)

        try:
            conn = sqlite3.connect(':memory:')
            cursor = conn.cursor()

            start_time = time.time()

            for stmt in statements[:-1]:
                cursor.execute(stmt)
            cursor.execute(statements[-1])

            # If the last statement produced rows (a SELECT, PRAGMA, etc.),
            # cursor.description is populated - fetch and format them.
            if cursor.description:
                results = cursor.fetchall()
                columns = [desc[0] for desc in cursor.description]

                # Format output as table
                output_lines = [" | ".join(columns)]
                output_lines.append("-" * len(output_lines[0]))
                for row in results:
                    output_lines.append(" | ".join(str(val) for val in row))
                output = "\n".join(output_lines)
            else:
                conn.commit()
                output = f"Query executed successfully. Rows affected: {cursor.rowcount}"

            execution_time = time.time() - start_time
            conn.close()

            return (True, output, None, execution_time)

        except sqlite3.OperationalError as e:
            return (False, None, f"SQL error: {str(e)}", 0.0)
        except Exception as e:
            return (False, None, f"Execution failed: {str(e)}", 0.0)

    @staticmethod
    def _split_sql_statements(script: str) -> "list[str]":
        """Splits a SQL script into individual statements on unquoted
        semicolons - simple quote-aware scan (tracks single/double quotes),
        not a full SQL parser. See execute_sql's docstring for why this
        exists (executescript() can't return the last statement's rows)."""
        statements: list[str] = []
        current: list[str] = []
        in_single = False
        in_double = False
        for ch in script:
            current.append(ch)
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif ch == ";" and not in_single and not in_double:
                stmt = "".join(current).strip()
                if stmt and stmt != ";":
                    statements.append(stmt)
                current = []
        trailing = "".join(current).strip()
        if trailing:
            statements.append(trailing)
        return statements


def execute_code(language: str, code: str) -> dict:
    """
    Main entry point for code execution.

    Args:
        language: "python", "javascript", or "sql"
        code: The code to execute

    Returns:
        dict with keys: success, output, error, execution_time
    """
    language = language.lower().strip()

    if language not in ["python", "javascript", "sql"]:
        return {
            "success": False,
            "output": None,
            "error": f"Unsupported language: {language}. Supported: python, javascript, sql",
            "execution_time": 0.0
        }

    if language == "python":
        success, output, error, exec_time = CodeExecutor.execute_python(code)
    elif language == "javascript":
        success, output, error, exec_time = CodeExecutor.execute_javascript(code)
    else:  # sql
        success, output, error, exec_time = CodeExecutor.execute_sql(code)

    return {
        "success": success,
        "output": output,
        "error": error,
        "execution_time": exec_time
    }
