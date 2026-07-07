"""End-to-end Front Door → governance ladder → gated agent step → completion.

The full product loop: a request enters the one door, is deterministically
routed to its matter-type ladder, the ladder's agent step lands as a PENDING
AgentDecision, a human approves it in the Cockpit (which advances the
ladder), humans walk the remaining steps, and the whole journey is on the
hash-chained audit ledger.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _front_door(client, description, *, context=None, request_type=None,
                      external_message_id=None, actor=None):
    headers = {"X-Dev-User-Email": actor} if actor else {}
    body = {
        "description": description,
        "requester_name": "Dana Li",
        "requester_email": "dana.li@aegis-demo.example",
        "department": "Sales",
    }
    if context is not None:
        body["context"] = context
    if request_type is not None:
        body["request_type"] = request_type
    if external_message_id is not None:
        body["external_message_id"] = external_message_id
    return await client.post("/api/v1/intake/front-door", json=body, headers=headers)


async def _pending_decision_for(client, instance_id):
    r = await client.get("/api/v1/cockpit/decisions?status=PENDING&limit=200")
    assert r.status_code == 200
    matches = [d for d in r.json() if d["resource_id"] == instance_id]
    return matches[0] if matches else None


async def _act(client, instance_id, action, **kw):
    return await client.post(
        f"/api/v1/workflow/instances/{instance_id}/actions",
        json={"action": action, **kw},
    )


async def test_nda_lane_end_to_end(client):
    # 1. Front Door routes free text to the NDA fast-track.
    r = await _front_door(
        client,
        "We need an NDA with Meridian Biotech for a co-development discussion.",
        context={"uses_standard_template": True},
    )
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["request_type"] == "nda"
    assert out["workflow_status"] == "in_progress"
    assert out["current_step"] == 1  # Request & Upload (human)
    iid = out["workflow_instance_id"]
    assert out["ticket"]["workflow_instance_id"] == iid  # ticket ↔ ladder link

    # 2. Human approves step 1 → ladder arrives at the AGENT step (2).
    r = await _act(client, iid, "approve", comment="Docs attached.")
    assert r.status_code == 200
    state = r.json()
    assert state["current_step_order"] == 2
    agent_steps = [s for s in state["rag"] if s["kind"] == "agent"]
    assert agent_steps and agent_steps[0]["step_order"] == 2

    # 3. The agent's proposal is a PENDING decision — the ladder has NOT moved.
    decision = await _pending_decision_for(client, iid)
    assert decision is not None
    assert decision["agent_id"] == "nda_reviewer"
    assert decision["action_payload"]["action"] == "approve"
    # NDA v2 decision tree: counterparty resolved in the ontology (Meridian
    # is seeded) but no prior NDA on file → template path, cited.
    assert decision["recommendation"]["confidence"] == 0.85
    assert any(
        c["type"] == "Counterparty" and "Meridian" in c["title"]
        for c in decision["recommendation"]["citations"]
    )

    r = await client.get(f"/api/v1/workflow/instances/{iid}")
    assert r.json()["current_step_order"] == 2  # still parked on the agent step

    # 4. Human approves the decision in the Cockpit → ladder advances to 3.
    r = await client.post(
        f"/api/v1/cockpit/decisions/{decision['id']}/approve", json={}
    )
    assert r.status_code == 200
    assert r.json()["status"] == "APPROVED"

    r = await client.get(f"/api/v1/workflow/instances/{iid}")
    assert r.json()["current_step_order"] == 3  # Legal Sign-off

    # 5. Walk the remaining human steps to completion.
    assert (await _act(client, iid, "approve")).json()["current_step_order"] == 4
    r = await _act(client, iid, "approve")  # E-Signature — last step
    assert r.json()["status"] == "completed"
    assert all(s["color"] in ("green", "skipped") for s in r.json()["rag"])

    # 6. The whole journey is chained and verifiable.
    r = await client.get("/api/v1/audit/verify")
    assert r.json()["ok"] is True
    r = await client.get("/api/v1/audit?limit=300")
    actions = {row["action"] for row in r.json()}
    assert {"workflow.instance.started", "workflow.instance.step_approved",
            "agent.decision.proposed", "agent.decision.approved"} <= actions


async def test_sanctions_hit_sends_back_after_human_approval(client):
    r = await _front_door(
        client,
        "Vendor onboarding for a new supplier in a high-risk geography.",
        context={"sanctions_hit": True},
    )
    out = r.json()
    assert out["request_type"] == "vendor"
    iid = out["workflow_instance_id"]

    # Step 1 (Vendor Details) approved → agent screening step proposes SEND BACK.
    await _act(client, iid, "approve")
    decision = await _pending_decision_for(client, iid)
    assert decision["agent_id"] == "counterparty_screener"
    assert decision["action_payload"]["action"] == "send_back"
    assert decision["action_payload"]["target_step"] == 1

    # Compliance approves the agent's send-back → ladder returns to step 1.
    r = await client.post(
        f"/api/v1/cockpit/decisions/{decision['id']}/approve", json={}
    )
    assert r.status_code == 200
    r = await client.get(f"/api/v1/workflow/instances/{iid}")
    state = r.json()
    assert state["current_step_order"] == 1
    # The screening step is red — it bounced the request.
    step2 = next(s for s in state["rag"] if s["step_order"] == 2)
    assert step2["color"] == "red"


async def test_skip_rule_excludes_finance_below_threshold(client):
    r = await _front_door(
        client,
        "Please review this services agreement.",
        request_type="contract",
        context={"contract_value": 5000, "risk_score": 1},
    )
    iid = r.json()["workflow_instance_id"]
    r = await client.get(f"/api/v1/workflow/instances/{iid}")
    finance = next(s for s in r.json()["rag"] if s["name"] == "Finance Review")
    assert finance["color"] == "skipped"


async def test_free_text_routes_to_regulatory(client):
    r = await _front_door(client, "We received a USFDA warning letter for the plant.")
    assert r.json()["request_type"] == "regulatory"


async def test_external_message_dedup(client):
    key = "msg-abc-123"
    r1 = await _front_door(client, "NDA with Foo Corp", external_message_id=key)
    r2 = await _front_door(client, "NDA with Foo Corp", external_message_id=key)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r2.json()["deduplicated"] is True
    assert r2.json()["ticket"]["id"] == r1.json()["ticket"]["id"]


async def test_requester_can_file_but_not_advance(client):
    r = await _front_door(
        client, "NDA with Bar Corp please", actor="requester@aegis-demo.example"
    )
    assert r.status_code == 201
    iid = r.json()["workflow_instance_id"]
    r = await client.post(
        f"/api/v1/workflow/instances/{iid}/actions",
        json={"action": "approve"},
        headers={"X-Dev-User-Email": "requester@aegis-demo.example"},
    )
    assert r.status_code == 403


async def test_stale_agent_decision_is_refused(client):
    # Park an instance on its agent step, then move the ladder out from
    # under the proposal via a human reject — the decision must be refused.
    r = await _front_door(client, "NDA with Stale Systems", context={})
    iid = r.json()["workflow_instance_id"]
    await _act(client, iid, "approve")  # → agent step 2, PENDING decision
    decision = await _pending_decision_for(client, iid)
    await _act(client, iid, "reject", comment="restarting")  # ladder → step 1

    r = await client.post(
        f"/api/v1/cockpit/decisions/{decision['id']}/approve", json={}
    )
    assert r.status_code == 409  # stale — the ladder moved since the proposal
