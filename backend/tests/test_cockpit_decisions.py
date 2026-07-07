"""Cockpit decisions HTTP API — approval queue over the governance gate.

Uses a LOCAL FastAPI app (main.py wiring for the cockpit router lands
separately) mounting only the cockpit router, exercised through the same
Postgres test database the session-scoped ``prepared_db`` fixture builds.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

from app.core import governance
from app.core.security import Actor
from app.db.models import AgentDecision, Role, User
from app.db.session import get_sessionmaker
from app.modules.cockpit.router import router as cockpit_router

pytestmark = pytest.mark.asyncio(loop_scope="session")

# Unique governed action for this test module — the registry raises on
# duplicate keys, so never reuse keys from other test files.
@governance.register_action("test.cockpit_echo")
async def _cockpit_echo(session, actor: Actor, decision: AgentDecision) -> dict:
    return {"ok": True}


test_app = FastAPI()
test_app.include_router(cockpit_router, prefix="/api/v1")


@pytest_asyncio.fixture(loop_scope="session")
async def cockpit_client(prepared_db):
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _admin_actor(session) -> Actor:
    user = (
        await session.execute(
            select(User).where(User.email == "alex.nguyen@aegis-demo.example")
        )
    ).scalars().first()
    role = await session.get(Role, user.role_id)
    return Actor(
        user_id=user.id,
        organization_id=user.organization_id,
        email=user.email,
        name=user.name,
        role_name=role.name,
        permissions=frozenset(role.permissions or []),
    )


async def _new_pending_decision() -> str:
    sm = get_sessionmaker()
    async with sm() as session:
        actor = await _admin_actor(session)
        decision = await governance.create_pending_decision(
            session,
            organization_id=actor.organization_id,
            agent_id="nda-agent",
            resource_type="IntakeTicket",
            resource_id="REQ-COCKPIT",
            action_key="test.cockpit_echo",
            action_payload={"response": "draft text"},
            recommendation={"confidence": 0.9, "suggested_action": "approve-and-send"},
        )
        await session.commit()
        return decision.id


async def test_list_pending_includes_fresh_decision(cockpit_client):
    decision_id = await _new_pending_decision()
    resp = await cockpit_client.get("/api/v1/cockpit/decisions?status=PENDING")
    assert resp.status_code == 200
    rows = resp.json()
    assert any(r["id"] == decision_id for r in rows)
    row = next(r for r in rows if r["id"] == decision_id)
    assert row["status"] == "PENDING"
    assert row["action_key"] == "test.cockpit_echo"
    assert row["recommendation"]["confidence"] == 0.9


async def test_get_by_id_and_bogus_404(cockpit_client):
    decision_id = await _new_pending_decision()
    resp = await cockpit_client.get(f"/api/v1/cockpit/decisions/{decision_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == decision_id

    resp = await cockpit_client.get("/api/v1/cockpit/decisions/does-not-exist")
    assert resp.status_code == 404


async def test_approve_via_http_then_conflict(cockpit_client):
    decision_id = await _new_pending_decision()
    resp = await cockpit_client.post(
        f"/api/v1/cockpit/decisions/{decision_id}/approve",
        json={"comment": "looks good"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "APPROVED"
    assert body["executed_audit_id"] is not None
    assert body["decided_by"] is not None

    # Exactly-once: a second approval is refused.
    resp = await cockpit_client.post(
        f"/api/v1/cockpit/decisions/{decision_id}/approve", json={}
    )
    assert resp.status_code == 409


async def test_approve_with_override(cockpit_client):
    decision_id = await _new_pending_decision()
    resp = await cockpit_client.post(
        f"/api/v1/cockpit/decisions/{decision_id}/approve",
        json={"payload_override": {"response": "human-edited"}, "comment": "edited"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "APPROVED_WITH_OVERRIDE"
    assert body["action_payload"] == {"response": "human-edited"}


async def test_reject_via_http(cockpit_client):
    decision_id = await _new_pending_decision()
    resp = await cockpit_client.post(
        f"/api/v1/cockpit/decisions/{decision_id}/reject",
        json={"comment": "not appropriate"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "REJECTED"
    assert body["decision_comment"] == "not appropriate"


async def test_rbac_requester_forbidden(cockpit_client):
    decision_id = await _new_pending_decision()
    headers = {"X-Dev-User-Email": "requester@aegis-demo.example"}

    resp = await cockpit_client.post(
        f"/api/v1/cockpit/decisions/{decision_id}/approve",
        json={},
        headers=headers,
    )
    assert resp.status_code == 403

    resp = await cockpit_client.get("/api/v1/cockpit/decisions", headers=headers)
    assert resp.status_code == 403
