"""Admin job triggers — HTTP entry points for scheduled passes.

The repo has no long-running worker yet, so these jobs run as authenticated
HTTP triggers (Vercel Cron / GitHub Actions / any scheduler can POST them).
Each job is idempotent and returns a structured summary, so swapping to a
pg-boss ``schedule()`` later is a runtime change, not a code change.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission
from app.core.security import Actor, require_permission
from app.db.session import get_session
from app.db.embeddings_index import reindex_org
from app.workflow.jobs import evaluate_sla_breaches

router = APIRouter(prefix="/admin/jobs", tags=["admin"])


class SlaSweepOut(BaseModel):
    organization_id: str
    instances_scanned: int
    breaches_recorded: int
    breached_instance_ids: list[str]


@router.post(
    "/sla-sweep",
    response_model=SlaSweepOut,
    summary="Record newly-breached workflow SLAs (idempotent; cron-triggerable).",
)
async def sla_sweep(
    session: AsyncSession = Depends(get_session),
    # Ops job — gated on audit read, the platform-observability grant.
    actor: Actor = Depends(require_permission(Permission.AUDIT_READ_ALL)),
) -> SlaSweepOut:
    result = await evaluate_sla_breaches(session, actor.organization_id)
    return SlaSweepOut(
        organization_id=result.organization_id,
        instances_scanned=result.instances_scanned,
        breaches_recorded=result.breaches_recorded,
        breached_instance_ids=result.breached_instance_ids,
    )


class ReindexOut(BaseModel):
    organization_id: str
    provider: str
    embedded: int
    skipped: int
    scanned: int


@router.post(
    "/reindex-embeddings",
    response_model=ReindexOut,
    summary="Rebuild ontology embeddings for GraphRAG's vector leg "
    "(idempotent via content hash; no-op when no provider is configured).",
)
async def reindex_embeddings(
    session: AsyncSession = Depends(get_session),
    actor: Actor = Depends(require_permission(Permission.AUDIT_READ_ALL)),
) -> ReindexOut:
    result = await reindex_org(session, actor.organization_id)
    return ReindexOut(
        organization_id=result.organization_id,
        provider=result.provider,
        embedded=result.embedded,
        skipped=result.skipped,
        scanned=result.scanned,
    )
