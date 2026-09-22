from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User, Skill
from app.schemas import SkillLearnIn, SkillOut
from app.auth import get_current_user
from app.ai_provider import get_ai_provider

router = APIRouter(prefix="/api/skills", tags=["skills"])


@router.post("/learn", response_model=SkillOut)
def learn_skill(
    payload: SkillLearnIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not settings.skill_engine_enabled:
        raise HTTPException(status_code=403, detail="The skill engine is disabled")

    topic = payload.topic.strip()
    if not topic:
        raise HTTPException(status_code=400, detail="Tell JARVIS what topic to learn")

    provider = get_ai_provider()
    try:
        result = provider.learn_skill(topic)
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="JARVIS couldn't research that topic right now - please try again",
        )

    skill = Skill(
        user_id=current_user.id,
        name=topic,
        description=result.get("description"),
        content=result.get("content") or "",
        status="pending_review",
        version=1,
    )
    db.add(skill)
    db.commit()
    db.refresh(skill)
    return skill


@router.get("", response_model=list[SkillOut])
def list_skills(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return (
        db.query(Skill)
        .filter(Skill.user_id == current_user.id)
        .order_by(Skill.created_at.desc())
        .all()
    )


@router.post("/{skill_id}/approve", response_model=SkillOut)
def approve_skill(
    skill_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    skill = (
        db.query(Skill)
        .filter(Skill.id == skill_id, Skill.user_id == current_user.id)
        .first()
    )
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    skill.status = "active"
    skill.approved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(skill)
    return skill


@router.delete("/{skill_id}")
def delete_skill(
    skill_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Covers both "discard a pending write-up JARVIS just researched" and
    # "forget a skill that's already active" - Sudeep can always re-learn the
    # same topic later if he wants a fresh write-up.
    skill = (
        db.query(Skill)
        .filter(Skill.id == skill_id, Skill.user_id == current_user.id)
        .first()
    )
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    db.delete(skill)
    db.commit()
    return {"status": "deleted"}
