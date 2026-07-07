"""The platform-wide conservative-AI gate — AgentDecision lifecycle.

Every agent recommendation that would mutate state is persisted as a PENDING
``AgentDecision`` carrying a *governed action*: a key into the registry below
plus a payload. The ONLY paths out of PENDING are:

- ``approve_decision`` — a human call that executes the registered action
  and writes the chain-sealed audit row **in the same transaction**. If the
  human edited the payload first, the status is APPROVED_WITH_OVERRIDE.
- ``reject_decision`` — a human call, also audited.

Downstream code must never execute an agent-proposed mutation outside this
path. The gate is enforced in schema + transactions, not in a prompt — a
prompt injection cannot remove a gate that isn't in the prompt.

Modules register their governed actions at import time::

    @register_action("intake.send_response")
    async def send_response(session, actor, decision) -> dict: ...

Transaction discipline: ``create_pending_decision`` does NOT commit (it
composes into the caller's mutation, e.g. ticket creation). ``approve_*`` /
``reject_*`` DO commit — they are the top-level human action.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import log_audit
from app.core.security import Actor
from app.db.models import AgentDecision

ActionExecutor = Callable[[AsyncSession, Actor, AgentDecision], Awaitable[dict]]

_ACTIONS: dict[str, ActionExecutor] = {}


class GovernanceError(Exception):
    """Base for gate violations → mapped to 4xx in routers."""


class DecisionNotFoundError(GovernanceError):
    pass


class DecisionNotPendingError(GovernanceError):
    """The decision already left PENDING — approve/reject exactly once."""


class UnknownActionError(GovernanceError):
    """No executor registered for the decision's action_key. Fails loud:
    an unexecutable approval must never silently no-op."""


def register_action(key: str) -> Callable[[ActionExecutor], ActionExecutor]:
    def deco(fn: ActionExecutor) -> ActionExecutor:
        if key in _ACTIONS:
            raise RuntimeError(f"Governed action '{key}' registered twice.")
        _ACTIONS[key] = fn
        return fn

    return deco


def registered_actions() -> tuple[str, ...]:
    return tuple(_ACTIONS.keys())


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def create_pending_decision(
    session: AsyncSession,
    *,
    organization_id: str,
    agent_id: str,
    resource_type: str,
    resource_id: str,
    action_key: str,
    action_payload: dict,
    recommendation: dict,
) -> AgentDecision:
    """Persist a PENDING decision + its AGENT-actor audit row. No commit —
    composes into the caller's mutation transaction."""
    if action_key not in _ACTIONS:
        # Locked contract: a decision must be executable the day a human
        # approves it. Registering the action is part of shipping the agent.
        raise UnknownActionError(
            f"Governed action '{action_key}' has no registered executor."
        )
    decision = AgentDecision(
        organization_id=organization_id,
        agent_id=agent_id,
        resource_type=resource_type,
        resource_id=resource_id,
        action_key=action_key,
        action_payload=action_payload,
        recommendation=recommendation,
        status="PENDING",
    )
    session.add(decision)
    await session.flush()
    await log_audit(
        session,
        organization_id=organization_id,
        actor_id=None,
        actor_type="AGENT",
        action="agent.decision.proposed",
        resource_type="AgentDecision",
        resource_id=decision.id,
        after_json={
            "agent_id": agent_id,
            "action_key": action_key,
            "status": "PENDING",
            "confidence": recommendation.get("confidence"),
            "degraded": recommendation.get("degraded", False),
        },
        metadata={"resource": f"{resource_type}:{resource_id}"},
    )
    return decision


async def _load_pending(
    session: AsyncSession, actor: Actor, decision_id: str
) -> AgentDecision:
    decision = await session.get(AgentDecision, decision_id)
    if decision is None or decision.organization_id != actor.organization_id:
        raise DecisionNotFoundError(f"AgentDecision {decision_id} not found.")
    if decision.status != "PENDING":
        raise DecisionNotPendingError(
            f"AgentDecision {decision_id} is {decision.status}; only PENDING "
            "decisions can be decided."
        )
    return decision


async def approve_decision(
    session: AsyncSession,
    actor: Actor,
    decision_id: str,
    *,
    payload_override: dict | None = None,
    comment: str | None = None,
) -> AgentDecision:
    """The ONLY path from PENDING to APPROVED / APPROVED_WITH_OVERRIDE.

    Executes the governed action and writes the audit row in the same
    transaction; if either fails, everything rolls back and the decision
    stays PENDING.
    """
    decision = await _load_pending(session, actor, decision_id)
    executor = _ACTIONS.get(decision.action_key)
    if executor is None:
        raise UnknownActionError(
            f"Governed action '{decision.action_key}' has no registered executor."
        )

    overridden = payload_override is not None
    if overridden:
        decision.action_payload = payload_override
    decision.status = "APPROVED_WITH_OVERRIDE" if overridden else "APPROVED"
    decision.decided_by = actor.user_id
    decision.decided_at = _now()
    decision.decision_comment = comment

    result = await executor(session, actor, decision)

    audit_id = await log_audit(
        session,
        organization_id=actor.organization_id,
        actor_id=actor.user_id,
        actor_type="USER",
        action="agent.decision.approved",
        resource_type="AgentDecision",
        resource_id=decision.id,
        before_json={"status": "PENDING"},
        after_json={"status": decision.status, "action_key": decision.action_key},
        metadata={
            "resource": f"{decision.resource_type}:{decision.resource_id}",
            "overridden": overridden,
            "execution_result": result or {},
        },
    )
    decision.executed_audit_id = audit_id
    await session.commit()
    await session.refresh(decision)
    return decision


async def reject_decision(
    session: AsyncSession,
    actor: Actor,
    decision_id: str,
    *,
    comment: str,
) -> AgentDecision:
    decision = await _load_pending(session, actor, decision_id)
    decision.status = "REJECTED"
    decision.decided_by = actor.user_id
    decision.decided_at = _now()
    decision.decision_comment = comment
    await log_audit(
        session,
        organization_id=actor.organization_id,
        actor_id=actor.user_id,
        actor_type="USER",
        action="agent.decision.rejected",
        resource_type="AgentDecision",
        resource_id=decision.id,
        before_json={"status": "PENDING"},
        after_json={"status": "REJECTED"},
        metadata={
            "resource": f"{decision.resource_type}:{decision.resource_id}",
            "comment": comment,
        },
    )
    await session.commit()
    await session.refresh(decision)
    return decision
