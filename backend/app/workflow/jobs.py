"""Scheduled workflow jobs — SLA sweep + maintenance.

Server-side SLA breach detection: a step that has waited past its
``sla_hours`` ages to red in the RAG bar (client-visible), but a breach is
only a first-class, alertable, audited fact once this sweep records it.
Written to be pg-boss / cron ready — the same shape as any scheduled pass:
idempotent within a step visit, returns a structured summary, takes an
``organization_id``.

Idempotency: each instance carries ``context["_sla_breached_step"]`` marking
the step visit already recorded, so re-running the sweep (or overlapping
cron fires) never double-writes. A send-back/approve that moves the ladder
clears the marker on the next transition (a fresh step visit can breach
again).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import log_audit
from app.db.models import Event, WorkflowInstance, WorkflowStep


@dataclass
class SlaSweepResult:
    organization_id: str
    instances_scanned: int
    breaches_recorded: int
    breached_instance_ids: list[str]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def evaluate_sla_breaches(
    session: AsyncSession, organization_id: str
) -> SlaSweepResult:
    """Record newly-breached SLAs for an org's in-progress ladders."""
    now = datetime.now(timezone.utc)
    instances = (
        await session.execute(
            select(WorkflowInstance).where(
                WorkflowInstance.organization_id == organization_id,
                WorkflowInstance.status == "in_progress",
            )
        )
    ).scalars().all()

    recorded: list[str] = []
    for inst in instances:
        step = (
            await session.execute(
                select(WorkflowStep).where(
                    WorkflowStep.definition_id == inst.definition_id,
                    WorkflowStep.step_order == inst.current_step_order,
                )
            )
        ).scalars().first()
        if step is None or not step.sla_hours or inst.step_entered_at is None:
            continue

        waited_h = (now - _aware(inst.step_entered_at)).total_seconds() / 3600
        if waited_h <= step.sla_hours:
            continue

        ctx = dict(inst.context or {})
        # Already recorded for THIS step visit? skip (idempotent).
        if ctx.get("_sla_breached_step") == inst.current_step_order:
            continue
        ctx["_sla_breached_step"] = inst.current_step_order
        inst.context = ctx

        session.add(
            Event(
                organization_id=organization_id,
                type="workflow.sla_breached",
                source_type="WorkflowInstance",
                source_id=inst.id,
                summary=(
                    f"Step {inst.current_step_order} '{step.name}' breached its "
                    f"{step.sla_hours}h SLA (waited {waited_h:.1f}h)."
                ),
                payload={
                    "step_order": inst.current_step_order,
                    "sla_hours": step.sla_hours,
                    "waited_hours": round(waited_h, 1),
                    "entity": f"{inst.entity_type}:{inst.entity_id}",
                },
            )
        )
        await log_audit(
            session,
            organization_id=organization_id,
            actor_id=None,
            actor_type="SYSTEM",
            action="workflow.sla_breached",
            resource_type="WorkflowInstance",
            resource_id=inst.id,
            after_json={
                "step_order": inst.current_step_order,
                "sla_hours": step.sla_hours,
                "waited_hours": round(waited_h, 1),
            },
            metadata={"entity": f"{inst.entity_type}:{inst.entity_id}",
                      "job": "sla_sweep"},
        )
        recorded.append(inst.id)

    await session.commit()
    return SlaSweepResult(
        organization_id=organization_id,
        instances_scanned=len(instances),
        breaches_recorded=len(recorded),
        breached_instance_ids=recorded,
    )
