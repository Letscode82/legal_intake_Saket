"""Workflow engine — the governance-ladder state machine.

Ported from the reference engine and reconciled with the non-negotiables:

  approve    -> next actionable step; approving the last step completes
  reject     -> hard reset to step 1 (first actionable)
  send_back  -> any previous step the actor selects
  cancel     -> terminate (audited)

Extras kept from the source: ``skip_if`` conditions on JSONB context, SLA
aging (amber → red past ``sla_hours``) surfaced via ``rag_status``, and an
optimistic ``version`` lock.

AEGIS changes:
- Org-scoped, async, cuid ids.
- Every transition twin-records to the hash-chained ``audit_log`` in the
  same transaction (``workflow.instance.*`` actions).
- Agent steps NEVER auto-apply. Arriving at a ``kind='agent'`` step runs the
  registered handler and persists its proposal as a PENDING ``AgentDecision``
  (governed action ``workflow.apply_agent_step``). The human approval in the
  Cockpit is what advances the ladder; a handler failure marks the task
  failed and leaves the step for a human — the ladder never stalls silently.
- No function here commits; callers (routers/services) own the transaction
  so a ladder movement, its audit row, and any decision row land atomically.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import governance
from app.core.audit import log_audit
from app.db.models import (
    WorkflowAgentTask,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowStep,
    WorkflowTransition,
)
from app.workflow.agents import WorkflowAgentDeps, get_workflow_agent
from app.workflow.hooks import completion_hooks_for

MAX_STEPS = 15

_AUDIT_ACTION = {
    "start": "workflow.instance.started",
    "approve": "workflow.instance.step_approved",
    "reject": "workflow.instance.rejected_to_start",
    "send_back": "workflow.instance.sent_back",
    "cancel": "workflow.instance.cancelled",
}


class WorkflowError(Exception):
    """Base — routers map to 4xx."""


class WorkflowConflictError(WorkflowError):
    """Terminal instance, stale version, or step no longer current."""


class WorkflowValidationError(WorkflowError):
    pass


# ── skip conditions — tiny rule language over instance context ─────────
_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "in": lambda a, b: a in b,
}


def _should_skip(step: WorkflowStep, context: dict) -> bool:
    rule = (step.payload_metadata or {}).get("skip_if")
    if not rule:
        return False
    try:
        value = context.get(rule["field"])
        return value is not None and _OPS[rule["op"]](value, rule["value"])
    except (KeyError, TypeError):
        return False  # a malformed rule never blocks the workflow


def _next_actionable(steps: list[WorkflowStep], after: int, context: dict) -> int | None:
    for s in steps:
        if s.step_order > after and not _should_skip(s, context):
            return s.step_order
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def start_instance(
    session: AsyncSession,
    *,
    organization_id: str,
    definition: WorkflowDefinition,
    entity_type: str,
    entity_id: str,
    started_by: str | None,
    started_by_label: str,
    context: dict | None = None,
) -> WorkflowInstance:
    if not definition.steps:
        raise WorkflowValidationError("Definition has no steps.")
    context = context or {}
    first = _next_actionable(definition.steps, after=0, context=context)
    if first is None:
        raise WorkflowValidationError(
            "All steps are skipped by conditions; nothing to run."
        )
    instance = WorkflowInstance(
        organization_id=organization_id,
        definition_id=definition.id,
        entity_type=entity_type,
        entity_id=entity_id,
        started_by=started_by,
        context=context,
        current_step_order=first,
    )
    session.add(instance)
    await session.flush()
    await _record(
        session, instance,
        from_step=0, to_step=first, action="start",
        actor_id=started_by, actor_label=started_by_label, comment=None,
    )
    await _maybe_run_agent_step(session, instance)
    return instance


async def act(
    session: AsyncSession,
    instance: WorkflowInstance,
    *,
    action: str,
    actor_id: str | None,
    actor_label: str,
    comment: str | None = None,
    target_step: int | None = None,
    expected_version: int | None = None,
) -> WorkflowInstance:
    if instance.status != "in_progress":
        raise WorkflowConflictError(f"Workflow is already {instance.status}.")
    if expected_version is not None and expected_version != instance.version:
        raise WorkflowConflictError(
            "Workflow changed since you loaded it — refresh and retry."
        )

    steps = instance.definition.steps
    current = instance.current_step_order
    context = instance.context or {}

    if action == "approve":
        to_step = _next_actionable(steps, after=current, context=context)
        if to_step is None:  # nothing actionable left → done
            instance.status = "completed"
    elif action == "reject":
        to_step = _next_actionable(steps, after=0, context=context) or 1
    elif action == "send_back":
        if target_step is None:
            raise WorkflowValidationError("send_back requires target_step.")
        if not (1 <= target_step < current):
            raise WorkflowValidationError(
                f"target_step must be a previous step (1..{current - 1})."
            )
        to_step = target_step
    elif action == "cancel":
        instance.status = "cancelled"
        to_step = None
    else:
        raise WorkflowValidationError(f"Unknown action '{action}'.")

    if to_step is not None:
        instance.current_step_order = to_step
        instance.step_entered_at = _now()
    instance.version += 1
    await _record(
        session, instance,
        from_step=current, to_step=to_step, action=action,
        actor_id=actor_id, actor_label=actor_label, comment=comment,
    )
    if instance.status == "completed":
        # Module-owned side effects (e.g. the NDA ladder authoring its
        # Document + NDA_WITH edge) run inside this same transaction.
        for hook in completion_hooks_for(instance.definition.key):
            await hook(session, instance)
    await _maybe_run_agent_step(session, instance)
    return instance


async def _record(
    session: AsyncSession,
    instance: WorkflowInstance,
    *,
    from_step: int,
    to_step: int | None,
    action: str,
    actor_id: str | None,
    actor_label: str,
    comment: str | None,
) -> None:
    """Twin-record: product-surface transition row + chain-sealed audit row."""
    session.add(
        WorkflowTransition(
            instance_id=instance.id,
            from_step_order=from_step,
            to_step_order=to_step,
            action=action,
            actor_id=actor_id,
            actor_label=actor_label,
            comment=comment,
        )
    )
    await log_audit(
        session,
        organization_id=instance.organization_id,
        actor_id=actor_id,
        actor_type="USER" if actor_id else "SYSTEM",
        action=_AUDIT_ACTION[action],
        resource_type="WorkflowInstance",
        resource_id=instance.id,
        after_json={
            "from_step": from_step,
            "to_step": to_step,
            "status": instance.status,
        },
        metadata={
            "entity": f"{instance.entity_type}:{instance.entity_id}",
            "actor_label": actor_label,
            "comment": comment,
        },
    )


async def _pending_agent_task(
    session: AsyncSession, instance_id: str, step_order: int
) -> WorkflowAgentTask | None:
    return (
        await session.execute(
            select(WorkflowAgentTask)
            .where(
                WorkflowAgentTask.instance_id == instance_id,
                WorkflowAgentTask.step_order == step_order,
                WorkflowAgentTask.status.in_(("pending", "awaiting_approval")),
            )
            .order_by(WorkflowAgentTask.created_at.desc())
        )
    ).scalars().first()


async def _maybe_run_agent_step(
    session: AsyncSession, instance: WorkflowInstance
) -> None:
    """If the ladder just arrived at an agent step, run the handler and
    persist its proposal as a PENDING AgentDecision (never auto-apply)."""
    if instance.status != "in_progress":
        return
    step = next(
        (s for s in instance.definition.steps
         if s.step_order == instance.current_step_order),
        None,
    )
    if step is None or step.kind != "agent":
        return
    if await _pending_agent_task(session, instance.id, step.step_order):
        return  # this step visit already has a live proposal

    agent_key = (step.agent_config or {}).get("agent_key", "")
    task = WorkflowAgentTask(
        instance_id=instance.id,
        step_order=step.step_order,
        input={"context": instance.context, "agent_key": agent_key},
    )
    session.add(task)
    await session.flush()

    handler = get_workflow_agent(agent_key)
    if handler is None:
        task.status = "failed"
        task.output = {"error": f"No workflow agent registered for '{agent_key}'."}
        task.finished_at = _now()
        return

    try:
        out = await handler(
            dict(instance.context or {}),
            dict(step.agent_config or {}),
            WorkflowAgentDeps(
                session=session, organization_id=instance.organization_id
            ),
        )
    except Exception as exc:  # noqa: BLE001 — a broken handler never stalls silently
        task.status = "failed"
        task.output = {"error": str(exc)[:500]}
        task.finished_at = _now()
        return

    decision = await governance.create_pending_decision(
        session,
        organization_id=instance.organization_id,
        agent_id=agent_key,
        resource_type="WorkflowInstance",
        resource_id=instance.id,
        action_key="workflow.apply_agent_step",
        action_payload={
            "instance_id": instance.id,
            "action": out.proposed_action,
            "target_step": out.target_step,
            "comment": out.comment,
            "expected_step": step.step_order,
        },
        recommendation={
            "confidence": out.confidence,
            "suggested_action": out.proposed_action,
            "drafted_response": out.drafted_response,
            "reasoning": out.comment,
            "concerns": (
                []
                if out.confidence
                >= float((step.agent_config or {}).get("min_confidence", 0.8))
                else [
                    "Agent confidence below the step threshold — review the "
                    "findings carefully before approving."
                ]
            ),
            "citations": out.citations,
            "degraded": False,
            "entity": f"{instance.entity_type}:{instance.entity_id}",
            "step_name": step.name,
        },
    )
    task.status = "awaiting_approval"
    task.decision_id = decision.id
    task.output = {
        "proposed_action": out.proposed_action,
        "target_step": out.target_step,
        "comment": out.comment,
        "confidence": out.confidence,
    }


@governance.register_action("workflow.apply_agent_step")
async def _apply_agent_step(session, actor, decision) -> dict:
    """Governed action: the human approval executes the agent's proposed
    ladder movement. Refuses if the ladder moved since the proposal."""
    payload = decision.action_payload
    instance = await session.get(WorkflowInstance, payload["instance_id"])
    if instance is None or instance.organization_id != actor.organization_id:
        raise governance.GovernanceError("Workflow instance not found.")
    # Refusals raise GovernanceError so ANY approval surface (Cockpit, future
    # module UIs) can map them to a 409 without knowing about this package.
    if instance.status != "in_progress":
        raise governance.GovernanceError(
            f"Workflow is already {instance.status}; the proposal is stale."
        )
    if instance.current_step_order != payload["expected_step"]:
        raise governance.GovernanceError(
            "The ladder moved since this proposal was made — the decision is stale."
        )
    await act(
        session,
        instance,
        action=payload["action"],
        actor_id=actor.user_id,
        actor_label=f"agent:{decision.agent_id} (approved by {actor.name})",
        comment=payload.get("comment"),
        target_step=payload.get("target_step"),
    )
    # Close out the agent task this decision came from.
    task = (
        await session.execute(
            select(WorkflowAgentTask).where(
                WorkflowAgentTask.decision_id == decision.id
            )
        )
    ).scalars().first()
    if task is not None:
        task.status = "done"
        task.finished_at = _now()
    return {
        "advanced_to": instance.current_step_order,
        "status": instance.status,
    }


def rag_status(instance: WorkflowInstance) -> list[dict]:
    """Red / Amber / Green per step for the wizard UI.

    green: passed (or workflow completed) · amber: awaiting action ·
    red: source of the latest reject/send-back not yet re-approved, OR the
    current step breached its SLA · grey: not reached · skipped: excluded
    by skip_if.
    """
    steps = instance.definition.steps
    current = instance.current_step_order
    completed = instance.status == "completed"
    context = instance.context or {}

    red_steps: set[int] = set()
    for t in instance.transitions:  # chronological
        if t.action in ("reject", "send_back"):
            red_steps.add(t.from_step_order)
        elif t.action == "approve":
            red_steps.discard(t.from_step_order)

    now = _now()
    out = []
    for s in steps:
        overdue = False
        if _should_skip(s, context):
            color = "skipped"
        elif completed or s.step_order < current:
            color = "green"
        elif s.step_order == current:
            color = "amber"
            if instance.status == "in_progress" and s.sla_hours and instance.step_entered_at:
                entered = instance.step_entered_at
                if entered.tzinfo is None:
                    entered = entered.replace(tzinfo=timezone.utc)
                if (now - entered).total_seconds() / 3600 > s.sla_hours:
                    color, overdue = "red", True
        else:
            color = "grey"
        if s.step_order in red_steps and not completed and color != "skipped":
            color = "red"
        out.append(
            {
                "step_order": s.step_order,
                "name": s.name,
                "screen_key": s.screen_key,
                "color": color,
                "overdue": overdue,
                "kind": s.kind,
            }
        )
    return out
