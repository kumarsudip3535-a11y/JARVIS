from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User, Memory
from app.schemas import MemoryCreate, MemoryOut, MemoryUpdate
from app.auth import get_current_user

router = APIRouter(prefix="/api/memory", tags=["memory"])


@router.get("", response_model=list[MemoryOut])
def list_memories(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return (
        db.query(Memory)
        .filter(Memory.user_id == current_user.id)
        .order_by(Memory.updated_at.desc())
        .all()
    )


@router.post("", response_model=MemoryOut)
def create_memory(
    payload: MemoryCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Memory content can't be empty")

    memory = Memory(
        user_id=current_user.id,
        content=content,
        category=payload.category,
        source="manual",
    )
    db.add(memory)
    db.commit()
    db.refresh(memory)
    return memory


@router.put("/{memory_id}", response_model=MemoryOut)
def update_memory(
    memory_id: int,
    payload: MemoryUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    memory = (
        db.query(Memory)
        .filter(Memory.id == memory_id, Memory.user_id == current_user.id)
        .first()
    )
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")

    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Memory content can't be empty")

    memory.content = content
    memory.category = payload.category
    memory.source = "manual"  # once Sudeep edits it by hand, treat it as his going forward
    db.commit()
    db.refresh(memory)
    return memory


@router.delete("/{memory_id}")
def delete_memory(
    memory_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    memory = (
        db.query(Memory)
        .filter(Memory.id == memory_id, Memory.user_id == current_user.id)
        .first()
    )
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")

    db.delete(memory)
    db.commit()
    return {"status": "deleted"}
