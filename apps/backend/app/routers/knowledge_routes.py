from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User, Document
from app.schemas import AskDocumentsIn, AskDocumentsOut, DocumentOut
from app.auth import get_current_user
from app.ai_provider import get_ai_provider
from app.document_utils import extract_docx_text, extract_xlsx_text

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

# ext -> (source_type stored on the Document row, mime type IF uploaded as-is).
# docx/xlsx have no "as-is" mime here because they're always converted to
# plain text first - see the upload handler below.
_EXTENSION_MAP = {
    "pdf": ("pdf", "application/pdf"),
    "txt": ("txt", "text/plain"),
    "csv": ("csv", "text/plain"),
    "docx": ("docx", None),
    "xlsx": ("xlsx", None),
}


@router.post("", response_model=DocumentOut)
async def upload_document(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not settings.knowledge_base_enabled:
        raise HTTPException(status_code=403, detail="Knowledge base is disabled")

    ext = (file.filename.rsplit(".", 1)[-1] if file.filename and "." in file.filename else "").lower()
    if ext not in _EXTENSION_MAP:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type - JARVIS currently reads PDF, Word (.docx), "
            "Excel (.xlsx), .txt and .csv files",
        )

    content = await file.read()
    max_bytes = settings.knowledge_max_upload_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"That file is too large - max {settings.knowledge_max_upload_mb}MB",
        )
    if not content:
        raise HTTPException(status_code=400, detail="That file is empty")

    source_type, upload_mime = _EXTENSION_MAP[ext]
    upload_filename = file.filename

    try:
        if ext == "docx":
            text = extract_docx_text(content)
            if not text:
                raise HTTPException(status_code=400, detail="Couldn't find any text in that Word document")
            content = text.encode("utf-8")
            upload_mime = "text/plain"
            upload_filename = file.filename.rsplit(".", 1)[0] + ".txt"
        elif ext == "xlsx":
            text = extract_xlsx_text(content)
            if not text:
                raise HTTPException(status_code=400, detail="Couldn't find any data in that Excel file")
            content = text.encode("utf-8")
            upload_mime = "text/plain"
            upload_filename = file.filename.rsplit(".", 1)[0] + ".txt"
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Couldn't read that file - it may be corrupted or password-protected",
        )

    provider = get_ai_provider()
    try:
        file_id = provider.upload_document(upload_filename, content, upload_mime)
    except Exception:
        raise HTTPException(status_code=502, detail="Upload to JARVIS's AI provider failed - please try again")

    document = Document(
        user_id=current_user.id,
        filename=file.filename,
        source_type=source_type,
        mime_type=upload_mime,
        anthropic_file_id=file_id,
        size_bytes=len(content),
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    return document


@router.get("", response_model=list[DocumentOut])
def list_documents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return (
        db.query(Document)
        .filter(Document.user_id == current_user.id)
        .order_by(Document.created_at.desc())
        .all()
    )


@router.delete("/{document_id}")
def delete_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    document = (
        db.query(Document)
        .filter(Document.id == document_id, Document.user_id == current_user.id)
        .first()
    )
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    provider = get_ai_provider()
    provider.delete_document(document.anthropic_file_id)

    db.delete(document)
    db.commit()
    return {"status": "deleted"}


@router.post("/ask", response_model=AskDocumentsOut)
def ask_about_documents(
    payload: AskDocumentsIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not payload.document_ids:
        raise HTTPException(status_code=400, detail="Pick at least one document to ask about")
    if not payload.question.strip():
        raise HTTPException(status_code=400, detail="Question can't be empty")

    documents = (
        db.query(Document)
        .filter(Document.id.in_(payload.document_ids), Document.user_id == current_user.id)
        .all()
    )
    if len(documents) != len(set(payload.document_ids)):
        raise HTTPException(status_code=404, detail="One or more documents were not found")

    provider = get_ai_provider()
    file_ids = [d.anthropic_file_id for d in documents]
    try:
        answer = provider.ask_about_documents(file_ids, payload.question)
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="JARVIS couldn't read those documents right now - please try again",
        )

    return {"answer": answer}
