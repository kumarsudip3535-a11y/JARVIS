import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User, Agent, Skill, Conversation, CustomTool
from app.schemas import AgentCreate, AgentUpdate, AgentOut
from app.auth import get_current_user

router = APIRouter(prefix="/api/agents", tags=["agents"])


def _to_out(agent: Agent) -> AgentOut:
    # AgentOut has no from_attributes Config (see schemas.py) because
    # assigned_skill_ids/assigned_custom_tool_ids are stored as JSON text
    # columns on the model but real lists on the API - this is the one place
    # that conversion happens.
    try:
        skill_ids = json.loads(agent.assigned_skill_ids or "[]")
    except (json.JSONDecodeError, TypeError):
        skill_ids = []
    try:
        tool_ids = json.loads(agent.assigned_custom_tool_ids or "[]")
    except (json.JSONDecodeError, TypeError):
        tool_ids = []
    return AgentOut(
        id=agent.id,
        name=agent.name,
        role_description=agent.role_description,
        system_instructions=agent.system_instructions,
        allow_web_search=agent.allow_web_search,
        allow_tally_billing=agent.allow_tally_billing,
        allow_email_calendar=agent.allow_email_calendar,
        assigned_skill_ids=skill_ids,
        assigned_custom_tool_ids=tool_ids,
        status=agent.status,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )


def _valid_skill_ids(db: Session, user_id: int, skill_ids: list[int]) -> list[int]:
    """Silently drops any id that isn't a real Skill belonging to this user,
    rather than erroring - a stale/bad id here should never block saving an
    agent. Only status="active" skills are ever actually recalled in chat
    (see chat_routes.py), so a paused/pending one can still be assigned in
    advance."""
    if not skill_ids:
        return []
    found = (
        db.query(Skill.id)
        .filter(Skill.user_id == user_id, Skill.id.in_(skill_ids))
        .all()
    )
    valid = {row[0] for row in found}
    # Preserve the order/dedupe the caller gave us, filtered to valid ids.
    seen = []
    for sid in skill_ids:
        if sid in valid and sid not in seen:
            seen.append(sid)
    return seen


def _valid_custom_tool_ids(db: Session, user_id: int, tool_ids: list[int]) -> list[int]:
    """Same silently-drop-invalid-ids behavior as _valid_skill_ids above -
    see its docstring. A paused/disabled tool can still be assigned in
    advance; only enabled=True tools are actually offered in chat (see
    chat_routes.py)."""
    if not tool_ids:
        return []
    found = (
        db.query(CustomTool.id)
        .filter(CustomTool.user_id == user_id, CustomTool.id.in_(tool_ids))
        .all()
    )
    valid = {row[0] for row in found}
    seen = []
    for tid in tool_ids:
        if tid in valid and tid not in seen:
            seen.append(tid)
    return seen


def _get_owned_agent(db: Session, agent_id: int, user_id: int) -> Agent:
    agent = (
        db.query(Agent)
        .filter(Agent.id == agent_id, Agent.user_id == user_id)
        .first()
    )
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


def _require_enabled():
    if not settings.agent_factory_enabled:
        raise HTTPException(status_code=403, detail="Agent Factory is disabled")


@router.post("", response_model=AgentOut)
def create_agent(
    payload: AgentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_enabled()

    name = payload.name.strip()
    role_description = payload.role_description.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Give the agent a name")
    if not role_description:
        raise HTTPException(status_code=400, detail="Describe what this agent is for")

    active_count = (
        db.query(Agent)
        .filter(Agent.user_id == current_user.id, Agent.status == "active")
        .count()
    )
    if active_count >= settings.agent_max_active:
        raise HTTPException(
            status_code=400,
            detail=(
                f"You already have {active_count} active agents (the limit is "
                f"{settings.agent_max_active}) - pause or delete one before "
                "creating another"
            ),
        )

    skill_ids = _valid_skill_ids(db, current_user.id, payload.assigned_skill_ids)
    tool_ids = _valid_custom_tool_ids(db, current_user.id, payload.assigned_custom_tool_ids)

    agent = Agent(
        user_id=current_user.id,
        name=name,
        role_description=role_description,
        system_instructions=(payload.system_instructions or "").strip() or None,
        allow_web_search=payload.allow_web_search,
        allow_tally_billing=payload.allow_tally_billing,
        allow_email_calendar=payload.allow_email_calendar,
        assigned_skill_ids=json.dumps(skill_ids),
        assigned_custom_tool_ids=json.dumps(tool_ids),
        status="active",
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return _to_out(agent)


@router.get("", response_model=list[AgentOut])
def list_agents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agents = (
        db.query(Agent)
        .filter(Agent.user_id == current_user.id)
        .order_by(Agent.created_at.desc())
        .all()
    )
    return [_to_out(a) for a in agents]


@router.get("/{agent_id}", response_model=AgentOut)
def get_agent(
    agent_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = _get_owned_agent(db, agent_id, current_user.id)
    return _to_out(agent)


@router.patch("/{agent_id}", response_model=AgentOut)
def update_agent(
    agent_id: int,
    payload: AgentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_enabled()
    agent = _get_owned_agent(db, agent_id, current_user.id)

    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Agent name can't be blank")
        agent.name = name
    if payload.role_description is not None:
        role_description = payload.role_description.strip()
        if not role_description:
            raise HTTPException(status_code=400, detail="Role description can't be blank")
        agent.role_description = role_description
    if payload.system_instructions is not None:
        agent.system_instructions = payload.system_instructions.strip() or None
    if payload.allow_web_search is not None:
        agent.allow_web_search = payload.allow_web_search
    if payload.allow_tally_billing is not None:
        agent.allow_tally_billing = payload.allow_tally_billing
    if payload.allow_email_calendar is not None:
        agent.allow_email_calendar = payload.allow_email_calendar
    if payload.assigned_skill_ids is not None:
        agent.assigned_skill_ids = json.dumps(
            _valid_skill_ids(db, current_user.id, payload.assigned_skill_ids)
        )
    if payload.assigned_custom_tool_ids is not None:
        agent.assigned_custom_tool_ids = json.dumps(
            _valid_custom_tool_ids(db, current_user.id, payload.assigned_custom_tool_ids)
        )

    db.commit()
    db.refresh(agent)
    return _to_out(agent)


@router.post("/{agent_id}/pause", response_model=AgentOut)
def pause_agent(
    agent_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Pausing only blocks starting NEW conversations with this agent (see
    # chat_routes.py) - conversations already tied to it keep working
    # normally for further messages, the same way a paused Skill isn't
    # retroactively un-learned.
    agent = _get_owned_agent(db, agent_id, current_user.id)
    agent.status = "paused"
    db.commit()
    db.refresh(agent)
    return _to_out(agent)


@router.post("/{agent_id}/resume", response_model=AgentOut)
def resume_agent(
    agent_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_enabled()
    agent = _get_owned_agent(db, agent_id, current_user.id)
    if agent.status != "active":
        active_count = (
            db.query(Agent)
            .filter(Agent.user_id == current_user.id, Agent.status == "active")
            .count()
        )
        if active_count >= settings.agent_max_active:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"You already have {active_count} active agents (the limit is "
                    f"{settings.agent_max_active}) - pause or delete one before "
                    "resuming this one"
                ),
            )
    agent.status = "active"
    db.commit()
    db.refresh(agent)
    return _to_out(agent)


@router.delete("/{agent_id}")
def delete_agent(
    agent_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = _get_owned_agent(db, agent_id, current_user.id)

    # Conversations already tied to this agent keep their message history -
    # just detach them (agent_id -> null) so they don't dangle on a deleted
    # row; they'll behave like ordinary general-JARVIS conversations for any
    # further messages.
    (
        db.query(Conversation)
        .filter(Conversation.agent_id == agent.id)
        .update({Conversation.agent_id: None})
    )

    db.delete(agent)
    db.commit()
    return {"status": "deleted"}
