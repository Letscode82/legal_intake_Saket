"""AgentDecision gate — the platform-wide conservative-AI contract.

Asserts the four load-bearing properties:
  1. PENDING is the only state an agent can create, and it is audited.
  2. Human approval is the only path that executes the governed action —
     and the action + audit row + status flip share one transaction
     (executor failure leaves the decision PENDING).
  3. A decision is decidable exactly once.
  4. Ontology edge writes compose into the approval transaction and are
     idempotent.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core import governance
from app.core.audit import verify_audit_chain
from app.core.security import Actor
from app.db.models import AgentDecision, AuditLog, Organization, Role, User
from app.db.ontology import NodeRef, add_edge, get_neighbors
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.asyncio(loop_scope="session")

_EXECUTED: list[dict] = []


@governance.register_action("test.echo")
async def _echo_action(session, actor: Actor, decision: AgentDecision) -> dict:
    # Compose an ontology write into the approval transaction, like a real
    # module action (e.g. the NDA approval writing NDA_WITH).
    await add_edge(
        session,
        organization_id=decision.organization_id,
        src=NodeRef("Counterparty", "cp-test"),
        label="NDA_WITH",
        dst=NodeRef("Organization", decision.organization_id),
        properties={"term_years": 2},
        source_module="test",
        created_by=actor.user_id,
    )
    _EXECUTED.append({"decision_id": decision.id, "payload": decision.action_payload})
    return {"echoed": True}


@governance.register_action("test.boom")
async def _boom_action(session, actor: Actor, decision: AgentDecision) -> dict:
    raise RuntimeError("executor exploded")


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


async def _new_pending(session, actor: Actor, action_key: str = "test.echo") -> str:
    decision = await governance.create_pending_decision(
        session,
        organization_id=actor.organization_id,
        agent_id="nda-agent",
        resource_type="IntakeTicket",
        resource_id="REQ-TEST",
        action_key=action_key,
        action_payload={"response": "draft text"},
        recommendation={"confidence": 0.9, "suggested_action": "approve-and-send"},
    )
    await session.commit()
    return decision.id


async def test_unregistered_action_is_refused(prepared_db):
    sm = get_sessionmaker()
    async with sm() as session:
        actor = await _admin_actor(session)
        with pytest.raises(governance.UnknownActionError):
            await governance.create_pending_decision(
                session,
                organization_id=actor.organization_id,
                agent_id="nda-agent",
                resource_type="IntakeTicket",
                resource_id="REQ-TEST",
                action_key="not.registered",
                action_payload={},
                recommendation={},
            )


async def test_pending_creation_is_audited(prepared_db):
    sm = get_sessionmaker()
    async with sm() as session:
        actor = await _admin_actor(session)
        decision_id = await _new_pending(session, actor)

        decision = await session.get(AgentDecision, decision_id)
        assert decision.status == "PENDING"
        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "agent.decision.proposed",
                    AuditLog.resource_id == decision_id,
                )
            )
        ).scalars().first()
        assert audit is not None
        assert audit.actor_type == "AGENT"
        assert audit.actor_id is None


async def test_approve_executes_action_and_audits_atomically(prepared_db):
    sm = get_sessionmaker()
    async with sm() as session:
        actor = await _admin_actor(session)
        decision_id = await _new_pending(session, actor)

        _EXECUTED.clear()
        decision = await governance.approve_decision(session, actor, decision_id)
        assert decision.status == "APPROVED"
        assert decision.decided_by == actor.user_id
        assert decision.executed_audit_id is not None
        assert len(_EXECUTED) == 1 and _EXECUTED[0]["decision_id"] == decision_id

        # The composed ontology edge landed in the same transaction.
        edges = await get_neighbors(
            session,
            organization_id=actor.organization_id,
            node=NodeRef("Counterparty", "cp-test"),
            labels=["NDA_WITH"],
            direction="out",
        )
        assert len(edges) == 1

        # Chain still verifies after the whole lifecycle.
        assert (await verify_audit_chain(session, actor.organization_id)).ok

        # Exactly-once: a second approval is refused.
        with pytest.raises(governance.DecisionNotPendingError):
            await governance.approve_decision(session, actor, decision_id)


async def test_override_approval_is_marked(prepared_db):
    sm = get_sessionmaker()
    async with sm() as session:
        actor = await _admin_actor(session)
        decision_id = await _new_pending(session, actor)
        decision = await governance.approve_decision(
            session, actor, decision_id,
            payload_override={"response": "human-edited text"},
            comment="tightened wording",
        )
        assert decision.status == "APPROVED_WITH_OVERRIDE"
        assert decision.action_payload == {"response": "human-edited text"}


async def test_executor_failure_leaves_decision_pending(prepared_db):
    sm = get_sessionmaker()
    async with sm() as session:
        actor = await _admin_actor(session)
        decision_id = await _new_pending(session, actor, action_key="test.boom")
        with pytest.raises(RuntimeError):
            await governance.approve_decision(session, actor, decision_id)
        await session.rollback()

    async with sm() as session:
        decision = await session.get(AgentDecision, decision_id)
        assert decision.status == "PENDING"  # nothing committed
        assert decision.executed_audit_id is None


async def test_reject_is_audited(prepared_db):
    sm = get_sessionmaker()
    async with sm() as session:
        actor = await _admin_actor(session)
        decision_id = await _new_pending(session, actor)
        decision = await governance.reject_decision(
            session, actor, decision_id, comment="not appropriate"
        )
        assert decision.status == "REJECTED"
        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "agent.decision.rejected",
                    AuditLog.resource_id == decision_id,
                )
            )
        ).scalars().first()
        assert audit is not None and audit.actor_type == "USER"


async def test_ontology_edge_idempotent_upsert(prepared_db):
    sm = get_sessionmaker()
    async with sm() as session:
        org = (
            await session.execute(
                select(Organization).where(Organization.name == "AEGIS Demo GC")
            )
        ).scalars().first()
        src, dst = NodeRef("Counterparty", "cp-idem"), NodeRef("Document", "doc-1")
        e1 = await add_edge(
            session, organization_id=org.id, src=src, label="PARTY_TO", dst=dst,
            properties={"a": 1}, source_module="test",
        )
        e2 = await add_edge(
            session, organization_id=org.id, src=src, label="PARTY_TO", dst=dst,
            properties={"b": 2}, source_module="test",
        )
        await session.commit()
        assert e1.id == e2.id  # same identity → same edge
        assert e2.properties == {"a": 1, "b": 2}  # properties merged

        both = await get_neighbors(
            session, organization_id=org.id, node=dst, direction="in"
        )
        assert any(e.id == e1.id for e in both)
