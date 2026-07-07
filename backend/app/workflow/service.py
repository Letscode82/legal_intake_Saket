"""Workflow service — library seeding and instance access."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import WorkflowDefinition, WorkflowInstance, WorkflowStep


async def seed_library(session: AsyncSession, organization_id: str) -> int:
    """Idempotently install the governance ladder library (v1) for an org.

    Returns the number of definitions installed this call. No commit —
    composes into the caller's transaction. Library edits ship as NEW
    versions (in-flight instances pin their definition row); v1 rows are
    never mutated here.
    """
    from app.workflow.library import WORKFLOW_LIBRARY

    existing = set(
        (
            await session.execute(
                select(WorkflowDefinition.key).where(
                    WorkflowDefinition.organization_id == organization_id,
                    WorkflowDefinition.version == 1,
                )
            )
        ).scalars()
    )
    installed = 0
    for d in WORKFLOW_LIBRARY:
        if d["key"] in existing:
            continue
        session.add(
            WorkflowDefinition(
                organization_id=organization_id,
                key=d["key"],
                version=1,
                name=d["name"],
                description=d.get("description"),
                steps=[
                    WorkflowStep(
                        step_order=st["step_order"],
                        name=st["name"],
                        screen_key=st["screen_key"],
                        approver_role=st["approver_role"],
                        kind=st["kind"],
                        agent_config=st["agent_config"],
                        sla_hours=st["sla_hours"],
                        payload_metadata=st["metadata"],
                    )
                    for st in d["steps"]
                ],
            )
        )
        installed += 1
    if installed:
        await session.flush()
    return installed


async def get_active_definition(
    session: AsyncSession, organization_id: str, key: str
) -> WorkflowDefinition | None:
    """Latest active version of a definition."""
    return (
        await session.execute(
            select(WorkflowDefinition)
            .where(
                WorkflowDefinition.organization_id == organization_id,
                WorkflowDefinition.key == key,
                WorkflowDefinition.is_active.is_(True),
            )
            .order_by(WorkflowDefinition.version.desc())
            .options(selectinload(WorkflowDefinition.steps))
        )
    ).scalars().first()


async def get_instance(
    session: AsyncSession, organization_id: str, instance_id: str
) -> WorkflowInstance | None:
    instance = await session.get(WorkflowInstance, instance_id)
    if instance is None or instance.organization_id != organization_id:
        return None
    return instance


async def get_instance_for_entity(
    session: AsyncSession, organization_id: str, entity_type: str, entity_id: str
) -> WorkflowInstance | None:
    return (
        await session.execute(
            select(WorkflowInstance)
            .where(
                WorkflowInstance.organization_id == organization_id,
                WorkflowInstance.entity_type == entity_type,
                WorkflowInstance.entity_id == entity_id,
            )
            .order_by(WorkflowInstance.created_at.desc())
        )
    ).scalars().first()
