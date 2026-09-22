import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User, CustomTool
from app.schemas import CustomToolCreate, CustomToolUpdate, CustomToolOut
from app.auth import get_current_user
from app import custom_tools

router = APIRouter(prefix="/api/tools", tags=["tools"])


def _to_out(tool: CustomTool) -> CustomToolOut:
    try:
        params = json.loads(tool.param_schema or "[]")
    except (json.JSONDecodeError, TypeError):
        params = []
    try:
        headers = json.loads(tool.static_headers or "{}")
    except (json.JSONDecodeError, TypeError):
        headers = {}
    return CustomToolOut(
        id=tool.id,
        name=tool.name,
        description=tool.description,
        http_method=tool.http_method,
        url=tool.url,
        param_schema=params,
        static_headers=headers,
        auth_header_name=tool.auth_header_name,
        has_auth_value=bool(tool.encrypted_auth_value),
        is_read_only=tool.is_read_only,
        enabled=tool.enabled,
        created_at=tool.created_at,
        updated_at=tool.updated_at,
    )


def _require_enabled():
    if not settings.tool_registry_enabled:
        raise HTTPException(status_code=403, detail="The tool registry is disabled")


def _get_owned_tool(db: Session, tool_id: int, user_id: int) -> CustomTool:
    tool = (
        db.query(CustomTool)
        .filter(CustomTool.id == tool_id, CustomTool.user_id == user_id)
        .first()
    )
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    return tool


@router.post("", response_model=CustomToolOut)
def create_tool(
    payload: CustomToolCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_enabled()

    name = payload.name.strip()
    description = payload.description.strip()
    url = payload.url.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Give the tool a name")
    if not description:
        raise HTTPException(status_code=400, detail="Describe what this tool does and when JARVIS should use it")
    if not url:
        raise HTTPException(status_code=400, detail="Give the tool's web address")

    method = (payload.http_method or "GET").strip().upper()
    if method not in custom_tools.ALLOWED_METHODS:
        raise HTTPException(status_code=400, detail=f"Unsupported method: {method}")

    encrypted_auth_value = None
    if payload.auth_value:
        try:
            encrypted_auth_value = custom_tools.encrypt_secret(payload.auth_value)
        except custom_tools.CustomToolError as e:
            raise HTTPException(status_code=400, detail=str(e))

    tool = CustomTool(
        user_id=current_user.id,
        name=name,
        description=description,
        http_method=method,
        url=url,
        param_schema=json.dumps([p.model_dump() for p in payload.param_schema]),
        static_headers=json.dumps(payload.static_headers or {}),
        auth_header_name=(payload.auth_header_name or "").strip() or None,
        encrypted_auth_value=encrypted_auth_value,
        is_read_only=payload.is_read_only,
        enabled=True,
    )
    db.add(tool)
    db.commit()
    db.refresh(tool)
    return _to_out(tool)


@router.get("", response_model=list[CustomToolOut])
def list_tools(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tools = (
        db.query(CustomTool)
        .filter(CustomTool.user_id == current_user.id)
        .order_by(CustomTool.created_at.desc())
        .all()
    )
    return [_to_out(t) for t in tools]


@router.get("/{tool_id}", response_model=CustomToolOut)
def get_tool(
    tool_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _to_out(_get_owned_tool(db, tool_id, current_user.id))


@router.patch("/{tool_id}", response_model=CustomToolOut)
def update_tool(
    tool_id: int,
    payload: CustomToolUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_enabled()
    tool = _get_owned_tool(db, tool_id, current_user.id)

    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Tool name can't be blank")
        tool.name = name
    if payload.description is not None:
        description = payload.description.strip()
        if not description:
            raise HTTPException(status_code=400, detail="Description can't be blank")
        tool.description = description
    if payload.http_method is not None:
        method = payload.http_method.strip().upper()
        if method not in custom_tools.ALLOWED_METHODS:
            raise HTTPException(status_code=400, detail=f"Unsupported method: {method}")
        tool.http_method = method
    if payload.url is not None:
        url = payload.url.strip()
        if not url:
            raise HTTPException(status_code=400, detail="URL can't be blank")
        tool.url = url
    if payload.param_schema is not None:
        tool.param_schema = json.dumps([p.model_dump() for p in payload.param_schema])
    if payload.static_headers is not None:
        tool.static_headers = json.dumps(payload.static_headers)
    if payload.auth_header_name is not None:
        tool.auth_header_name = payload.auth_header_name.strip() or None
    if payload.clear_auth:
        tool.encrypted_auth_value = None
        if payload.auth_header_name is None:
            tool.auth_header_name = None
    elif payload.auth_value:
        try:
            tool.encrypted_auth_value = custom_tools.encrypt_secret(payload.auth_value)
        except custom_tools.CustomToolError as e:
            raise HTTPException(status_code=400, detail=str(e))
    if payload.is_read_only is not None:
        tool.is_read_only = payload.is_read_only
    if payload.enabled is not None:
        tool.enabled = payload.enabled

    db.commit()
    db.refresh(tool)
    return _to_out(tool)


@router.delete("/{tool_id}")
def delete_tool(
    tool_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tool = _get_owned_tool(db, tool_id, current_user.id)
    db.delete(tool)
    db.commit()
    return {"status": "deleted"}
