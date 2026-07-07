"""Audit ledger read + defensibility surface."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import verify_audit_chain
from app.core.permissions import Permission
from app.core.security import Actor, require_permission
from app.db.models import AuditLog
from app.db.session import get_session
from app.modules.intake.schemas import ChainVerificationOut

router = APIRouter(prefix="/audit", tags=["audit"])


class AuditRowOut(BaseModel):
    id: str
    actor_id: str | None
    actor_type: str
    action: str
    resource_type: str
    resource_id: str
    chain_position: int
    content_hash: str
    prev_hash: str
    timestamp: datetime


@router.get(
    "/verify",
    response_model=ChainVerificationOut,
    summary="Verify the organization's audit chain is intact (tamper-evident).",
)
async def verify(
    session: AsyncSession = Depends(get_session),
    actor: Actor = Depends(require_permission(Permission.AUDIT_READ_ALL)),
) -> ChainVerificationOut:
    result = await verify_audit_chain(session, actor.organization_id)
    return ChainVerificationOut(
        ok=result.ok,
        rows_checked=result.rows_checked,
        broken_at_position=result.broken_at_position,
        reason=result.reason,
        problems=result.problems,
    )


@router.get(
    "",
    response_model=list[AuditRowOut],
    summary="List the organization's audit ledger (newest first).",
)
async def list_audit(
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
    actor: Actor = Depends(require_permission(Permission.AUDIT_READ_ALL)),
) -> list[AuditRowOut]:
    rows = (
        await session.execute(
            select(AuditLog)
            .where(AuditLog.organization_id == actor.organization_id)
            .order_by(AuditLog.chain_position.desc())
            .limit(min(max(limit, 1), 500))
        )
    ).scalars().all()
    return [
        AuditRowOut(
            id=r.id,
            actor_id=r.actor_id,
            actor_type=r.actor_type,
            action=r.action,
            resource_type=r.resource_type,
            resource_id=r.resource_id,
            chain_position=int(r.chain_position),
            content_hash=r.content_hash,
            prev_hash=r.prev_hash,
            timestamp=r.timestamp,
        )
        for r in rows
    ]
