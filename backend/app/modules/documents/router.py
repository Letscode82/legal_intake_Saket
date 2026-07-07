"""Document upload/read HTTP surface — shared Document entity only.

Limits: 5 MB per file; allow-listed MIME types. Plain-text formats are
decoded (capped) into ``extracted_text``; binary formats (PDF/DOCX) store
with ``extracted_text=None`` until a real parser ships — agents surface
that as a gap rather than hallucinating content. Every upload is audited.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import log_audit
from app.core.ids import gen_id
from app.core.permissions import Permission
from app.core.security import Actor, get_current_actor
from app.db.models import Document
from app.db.session import get_session

router = APIRouter(prefix="/documents", tags=["documents"])

MAX_BYTES = 5 * 1024 * 1024
MAX_EXTRACT_CHARS = 200_000

_TEXT_MIMES = {"text/plain", "text/markdown", "text/csv"}
_BINARY_MIMES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_ALLOWED_MIMES = _TEXT_MIMES | _BINARY_MIMES

# Which permission authorizes attaching a document to which owner type.
_OWNER_PERMISSION: dict[str, Permission] = {
    "INTAKE": Permission.INTAKE_CREATE_TICKET,
    "CONTRACT": Permission.CONTRACTS_CREATE,
    "MATTER": Permission.MATTER_UPDATE,
}


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    mime_type: str
    size_bytes: int
    owner_type: str
    owner_id: str
    uploaded_by: str
    uploaded_at: datetime
    has_extracted_text: bool = False


def _out(doc: Document) -> DocumentOut:
    out = DocumentOut.model_validate(doc)
    out.has_extracted_text = bool(doc.extracted_text)
    return out


@router.post(
    "",
    response_model=DocumentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a document onto a shared-entity owner (untrusted input; "
    "size/type limited; audited).",
)
async def upload_document(
    file: UploadFile = File(...),
    owner_type: Literal["INTAKE", "CONTRACT", "MATTER"] = Form(...),
    owner_id: str = Form(..., min_length=1, max_length=100),
    session: AsyncSession = Depends(get_session),
    actor: Actor = Depends(get_current_actor),
) -> DocumentOut:
    needed = _OWNER_PERMISSION[owner_type]
    if needed.value not in actor.permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission {needed.value}.",
        )

    mime = (file.content_type or "").split(";")[0].strip().lower()
    if mime not in _ALLOWED_MIMES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"MIME type {mime!r} is not allowed.",
        )

    payload = await file.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"File exceeds the {MAX_BYTES // (1024 * 1024)} MB limit.",
        )
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file."
        )

    extracted: str | None = None
    if mime in _TEXT_MIMES:
        # Decode as data. Never executed, never interpreted — downstream
        # prompts must fence this via untrusted-content spotlighting.
        extracted = payload.decode("utf-8", errors="replace")[:MAX_EXTRACT_CHARS]

    doc = Document(
        organization_id=actor.organization_id,
        name=(file.filename or "upload")[:255],
        mime_type=mime,
        size_bytes=len(payload),
        # Demo-grade inline storage; a blob store swaps in behind this URL.
        storage_url=f"inline://{gen_id('doc')}",
        owner_type=owner_type,
        owner_id=owner_id,
        uploaded_by=actor.user_id,
        extracted_text=extracted,
    )
    session.add(doc)
    await session.flush()
    await log_audit(
        session,
        organization_id=actor.organization_id,
        actor_id=actor.user_id,
        actor_type="USER",
        action="document.uploaded",
        resource_type="Document",
        resource_id=doc.id,
        after_json={
            "name": doc.name,
            "mime_type": mime,
            "size_bytes": doc.size_bytes,
            "owner": f"{owner_type}:{owner_id}",
            "extracted": extracted is not None,
        },
    )
    await session.commit()
    await session.refresh(doc)
    return _out(doc)


@router.get("/{document_id}", response_model=DocumentOut, summary="Document metadata.")
async def get_document(
    document_id: str,
    session: AsyncSession = Depends(get_session),
    actor: Actor = Depends(get_current_actor),
) -> DocumentOut:
    doc = await session.get(Document, document_id)
    if doc is None or doc.organization_id != actor.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    needed = _OWNER_PERMISSION.get(doc.owner_type, Permission.CONTRACTS_READ_ALL)
    read_ok = (
        Permission.CONTRACTS_READ_ALL.value in actor.permissions
        or needed.value in actor.permissions
    )
    if not read_ok:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted.")
    return _out(doc)
