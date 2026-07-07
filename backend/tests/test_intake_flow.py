"""Legal Intake NDA path — deterministic spine + human-approval gate + RBAC."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")

_NDA_BODY = {
    "description": "Please prepare an NDA with Acme Corp for a partnership discussion.",
    "requester_name": "Dana Li",
    "requester_email": "dana.li@aegis-demo.example",
    "department": "Sales",
}


async def _create(client, body=None, actor_email=None):
    headers = {"X-Dev-User-Email": actor_email} if actor_email else {}
    return await client.post("/api/v1/intake/tickets", json=body or _NDA_BODY, headers=headers)


async def test_create_classifies_and_recommends_pending(client):
    r = await _create(client)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["ticket"]["type"] == "NDA Request"
    assert data["ticket"]["status"] == "IN_REVIEW"
    rec = data["recommendation"]
    assert rec is not None
    assert rec["agent_id"] == "nda-agent"
    # The gate: the agent only ever produces PENDING.
    assert rec["status"] == "PENDING"


async def test_degraded_fallback_never_auto_sends(client):
    # No ANTHROPIC_API_KEY in the test env → degraded path.
    r = await _create(client)
    rec = r.json()["recommendation"]
    assert rec["suggested_action"] == "flag-for-review"
    assert rec["confidence"] == 0.4
    assert any("AI review unavailable" in c for c in rec["concerns"])


async def test_human_approval_is_the_only_path_to_approved(client):
    tid = (await _create(client)).json()["ticket"]["id"]
    r = await client.post(f"/api/v1/intake/tickets/{tid}/approve", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ticket"]["status"] == "APPROVED"
    assert body["recommendation"]["status"] == "APPROVED"
    assert body["recommendation"]["reviewed_by"] is not None

    # No pending rec remains → second approve is a conflict.
    r2 = await client.post(f"/api/v1/intake/tickets/{tid}/approve", json={})
    assert r2.status_code == 409


async def test_edited_approval_is_recorded_as_edited(client):
    tid = (await _create(client)).json()["ticket"]["id"]
    r = await client.post(
        f"/api/v1/intake/tickets/{tid}/approve",
        json={"edited_response": "Edited by attorney before sending."},
    )
    assert r.status_code == 200
    rec = r.json()["recommendation"]
    assert rec["status"] == "EDITED"
    assert rec["drafted_response"] == "Edited by attorney before sending."


async def test_reject_flow(client):
    tid = (await _create(client)).json()["ticket"]["id"]
    r = await client.post(
        f"/api/v1/intake/tickets/{tid}/reject", json={"reason": "Out of scope."}
    )
    assert r.status_code == 200
    assert r.json()["ticket"]["status"] == "REJECTED"
    assert r.json()["recommendation"]["status"] == "REJECTED"


async def test_requester_cannot_approve(client):
    # A requester can file but lacks intake:approve_recommendation.
    tid = (
        await _create(client, actor_email="requester@aegis-demo.example")
    ).json()["ticket"]["id"]
    r = await client.post(
        "/api/v1/intake/tickets/%s/approve" % tid,
        json={},
        headers={"X-Dev-User-Email": "requester@aegis-demo.example"},
    )
    assert r.status_code == 403


async def test_approved_flow_is_fully_audited_and_chain_intact(client):
    tid = (await _create(client)).json()["ticket"]["id"]
    await client.post(f"/api/v1/intake/tickets/{tid}/approve", json={})

    r = await client.get("/api/v1/audit/verify")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    r = await client.get("/api/v1/audit?limit=200")
    actions = {row["action"] for row in r.json()}
    assert "intake.ticket.created" in actions
    assert "intake.recommendation.generated" in actions
    assert "intake.recommendation.approved" in actions
