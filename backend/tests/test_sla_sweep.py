"""SLA breach sweep job (PR 20) — records breaches, idempotent, resets on move."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.modules.contracts.agents  # noqa: F401 — register handlers
from app.core.security import Actor
from app.db.models import Event, Role, User, WorkflowInstance
from app.db.session import get_sessionmaker
from app.workflow import engine, service
from app.workflow.jobs import evaluate_sla_breaches

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _admin(session) -> Actor:
    u = (
        await session.execute(
            select(User).where(User.email == "alex.nguyen@aegis-demo.example")
        )
    ).scalars().first()
    r = await session.get(Role, u.role_id)
    return Actor(
        user_id=u.id, organization_id=u.organization_id, email=u.email,
        name=u.name, role_name=r.name, permissions=frozenset(r.permissions or []),
    )


async def _start_and_age(session, actor) -> WorkflowInstance:
    """Start an NDA ladder and backdate step entry past the step-2 SLA."""
    await service.seed_library(session, actor.organization_id)
    definition = await service.get_active_definition(
        session, actor.organization_id, "nda_fasttrack"
    )
    inst = await engine.start_instance(
        session, organization_id=actor.organization_id, definition=definition,
        entity_type="IntakeTicket", entity_id="REQ-SLA-TEST",
        started_by=actor.user_id, started_by_label=actor.name,
        context={"uses_standard_template": True},
    )
    # Move to step 2 (AI Template Review, sla=4h) then backdate entry 10h.
    await engine.act(session, inst, action="approve", actor_id=actor.user_id,
                     actor_label=actor.name)
    inst.step_entered_at = datetime.now(timezone.utc) - timedelta(hours=10)
    await session.commit()
    return inst


async def test_sweep_records_breach_once_then_is_idempotent(prepared_db):
    sm = get_sessionmaker()
    async with sm() as session:
        actor = await _admin(session)
        inst = await _start_and_age(session, actor)
        iid = inst.id

    async with sm() as session:
        actor = await _admin(session)
        r1 = await evaluate_sla_breaches(session, actor.organization_id)
        assert iid in r1.breached_instance_ids
        assert r1.breaches_recorded >= 1

    async with sm() as session:
        actor = await _admin(session)
        r2 = await evaluate_sla_breaches(session, actor.organization_id)
        assert iid not in r2.breached_instance_ids  # already recorded — idempotent

    async with sm() as session:
        events = (
            await session.execute(
                select(Event).where(
                    Event.type == "workflow.sla_breached", Event.source_id == iid
                )
            )
        ).scalars().all()
        assert len(events) == 1  # exactly one breach event for this step visit


async def test_admin_endpoint_gated_and_functional(client):
    # Requester lacks audit:read_all → 403.
    r = await client.post(
        "/api/v1/admin/jobs/sla-sweep",
        headers={"X-Dev-User-Email": "requester@aegis-demo.example"},
    )
    assert r.status_code == 403
    # Admin → 200 with a structured summary.
    r = await client.post("/api/v1/admin/jobs/sla-sweep")
    assert r.status_code == 200
    body = r.json()
    assert "instances_scanned" in body and "breaches_recorded" in body
