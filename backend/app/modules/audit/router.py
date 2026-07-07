"""Audit ledger read + defensibility surface."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, text
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


class ExportRowOut(BaseModel):
    id: str
    chain_position: int
    actor_id: str | None
    actor_type: str
    action: str
    resource_type: str
    resource_id: str
    timestamp: datetime
    prev_hash: str
    content_hash: str
    schema_version: int
    # The exact string the trigger hashed — an off-database auditor can
    # SHA-256 this and compare to content_hash without reproducing JSONB
    # normalization.
    canonical_content: str


class DefensibilityExportOut(BaseModel):
    schema_id: str = "aegis.audit.defensibility.v1"
    organization_id: str
    generated_at: datetime
    verification: ChainVerificationOut
    row_count: int
    rows: list[ExportRowOut]


@router.get(
    "/export",
    response_model=DefensibilityExportOut,
    summary="Defensibility export — every row with its verbatim canonical "
    "content, so auditors can re-verify the chain off-database.",
)
async def export_defensibility(
    session: AsyncSession = Depends(get_session),
    actor: Actor = Depends(require_permission(Permission.AUDIT_READ_ALL)),
) -> DefensibilityExportOut:
    verification = await verify_audit_chain(session, actor.organization_id)

    rows = (
        await session.execute(
            text(
                """
                SELECT id, chain_position, actor_id, actor_type, action,
                       resource_type, resource_id, timestamp, prev_hash,
                       content_hash, schema_version,
                       audit_log_canonical_content(
                           schema_version, organization_id, actor_id,
                           actor_type, action, resource_type, resource_id,
                           before_json, after_json, metadata, timestamp,
                           prev_hash, chain_position
                       ) AS canonical_content
                FROM audit_log
                WHERE organization_id = :org
                ORDER BY chain_position ASC
                """
            ),
            {"org": actor.organization_id},
        )
    ).mappings().all()

    export_rows = [
        ExportRowOut(
            id=r["id"],
            chain_position=int(r["chain_position"]),
            actor_id=r["actor_id"],
            actor_type=r["actor_type"],
            action=r["action"],
            resource_type=r["resource_type"],
            resource_id=r["resource_id"],
            timestamp=r["timestamp"],
            prev_hash=r["prev_hash"],
            content_hash=r["content_hash"],
            schema_version=int(r["schema_version"]),
            canonical_content=r["canonical_content"],
        )
        for r in rows
    ]
    return DefensibilityExportOut(
        organization_id=actor.organization_id,
        generated_at=datetime.now(timezone.utc),
        verification=ChainVerificationOut(
            ok=verification.ok,
            rows_checked=verification.rows_checked,
            broken_at_position=verification.broken_at_position,
            reason=verification.reason,
            problems=verification.problems,
        ),
        row_count=len(export_rows),
        rows=export_rows,
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
