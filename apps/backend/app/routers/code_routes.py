"""
Code execution API routes.
Allows JARVIS to execute Python, JavaScript, and SQL code safely in a sandbox.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import User
from app.schemas import CodeExecuteIn, CodeExecuteOut
from app.code_executor import execute_code

router = APIRouter(prefix="/code", tags=["code"])


@router.post("/execute", response_model=CodeExecuteOut)
def execute_user_code(
    request: CodeExecuteIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Execute code in a sandboxed environment.

    Supported languages:
    - python: runs inside a fresh, disposable Docker container (network
      disabled, memory/CPU/process limits, read-only filesystem) - see
      code_executor.py's module docstring for the full 2026-09-20 rewrite
      that replaced the original text-blocklist approach, which was
      trivially bypassable and never provided real isolation.
    - javascript: same Docker-container isolation, via Node.js.
    - sql: runs against an in-memory SQLite database (unchanged - never
      had the same vulnerability; see code_executor.py).

    Security: Python/JavaScript execution requires Docker to actually be
    reachable (Docker Desktop running) - if it isn't, execution is refused
    with a clear error rather than silently falling back to running the
    real interpreter directly on this host.
    """
    if not request.code or not request.code.strip():
        raise HTTPException(status_code=400, detail="Code cannot be empty")

    if request.language.lower() not in ["python", "javascript", "sql"]:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported language: {request.language}. Supported: python, javascript, sql"
        )

    # Execute the code
    result = execute_code(request.language, request.code)

    return CodeExecuteOut(
        success=result["success"],
        output=result["output"],
        error=result["error"],
        execution_time=result["execution_time"]
    )
