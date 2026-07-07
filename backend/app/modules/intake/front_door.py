"""The Legal Front Door — one intake, many governance ladders.

Every request reaching Legal comes through here: an explicit ``request_type``
routes directly; free text is triaged by the deterministic classifier
(``app.workflow.library.classify`` — the spine, never an LLM). A ticket is
created, the matter-type governance ladder starts, and if the ladder's first
actionable step is an agent step, that agent's proposal lands as a PENDING
AgentDecision for the Cockpit — nothing acts until a human approves.

Dedup: an ``external_message_id`` (email internetMessageId, webhook
messageId) resolves a retry/replay to the existing ticket instead of
creating a duplicate.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import log_audit
from app.core.security import Actor
from app.db.models import IntakeTicket, WorkflowInstance
from app.modules.intake.agents.classifier import classify_intake
from app.modules.intake.service import (
    _next_ticket_id,
    _resolve_or_provision_requester,
)
from app.workflow import engine as wf_engine
from app.workflow import service as wf_service
from app.workflow.library import REQUEST_TYPES, classify


class UnknownRequestTypeError(Exception):
    pass


async def submit_front_door_request(
    session: AsyncSession,
    actor: Actor,
    *,
    description: str,
    requester_name: str,
    requester_email: str | None = None,
    department: str | None = None,
    source: str = "FORM",
    request_type: str | None = None,
    external_message_id: str | None = None,
    context: dict | None = None,
) -> tuple[IntakeTicket, WorkflowInstance, str, float, bool]:
    """Returns (ticket, instance, request_type, confidence, deduplicated)."""

    # 0. Dedup — a replayed message resolves to its existing ticket.
    if external_message_id:
        existing = (
            await session.execute(
                select(IntakeTicket).where(
                    IntakeTicket.organization_id == actor.organization_id,
                    IntakeTicket.external_message_id == external_message_id,
                )
            )
        ).scalars().first()
        if existing is not None:
            instance = await wf_service.get_instance(
                session, actor.organization_id, existing.workflow_instance_id or ""
            )
            lane = (existing.ai_triage_json or {}).get("request_type", "contract")
            return existing, instance, lane, 1.0, True

    # 1. Deterministic lane routing (the spine).
    confidence = 1.0
    if not request_type:
        request_type, confidence = classify(description)
    route = REQUEST_TYPES.get(request_type)
    if route is None:
        raise UnknownRequestTypeError(f"Unknown request_type '{request_type}'.")

    # 2. Ensure the ladder library is installed for this org, then resolve
    #    the lane's definition.
    await wf_service.seed_library(session, actor.organization_id)
    definition = await wf_service.get_active_definition(
        session, actor.organization_id, route["definition_key"]
    )

    # 3. Ticket fields from the deterministic intake classifier (priority /
    #    SLA), requester resolution (explicit, audited provisioning).
    classification = classify_intake(description, hint_type=None)
    requester = await _resolve_or_provision_requester(
        session, actor=actor, name=requester_name, email=requester_email
    )
    ticket_id = await _next_ticket_id(session, actor.organization_id)

    ticket = IntakeTicket(
        id=ticket_id,
        organization_id=actor.organization_id,
        requester_id=requester.id,
        source=source,
        type=route["label"],
        priority=classification.priority,
        status="IN_REVIEW",
        stage="routed",
        description=description,
        department=department,
        assigned_to="Front Door",
        sla_hours=classification.sla_hours,
        sla_status="On Track",
        ai_triage_json={
            "request_type": request_type,
            "confidence": confidence,
            "definition_key": route["definition_key"],
        },
        external_message_id=external_message_id,
    )
    session.add(ticket)
    await session.flush()

    await log_audit(
        session,
        organization_id=actor.organization_id,
        actor_id=actor.user_id,
        actor_type="USER",
        action="intake.ticket.created",
        resource_type="IntakeTicket",
        resource_id=ticket.id,
        after_json={
            "type": ticket.type,
            "priority": ticket.priority,
            "source": source,
            "request_type": request_type,
            "routing_confidence": confidence,
        },
        metadata={"channel": "front_door"},
    )

    # 4. Start the governance ladder. If its first actionable step is an
    #    agent step, the engine persists a PENDING AgentDecision here too —
    #    all in this one transaction.
    ladder_context = {
        "description": description,
        "request_type": request_type,
        **(context or {}),
    }
    instance = await wf_engine.start_instance(
        session,
        organization_id=actor.organization_id,
        definition=definition,
        entity_type="IntakeTicket",
        entity_id=ticket.id,
        started_by=actor.user_id,
        started_by_label=actor.name,
        context=ladder_context,
    )
    ticket.workflow_instance_id = instance.id

    await session.commit()
    await session.refresh(ticket)
    await session.refresh(instance)
    return ticket, instance, request_type, confidence, False
