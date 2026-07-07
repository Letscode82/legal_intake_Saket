"""Legal Intake service layer — logic + audit writes in one transaction.

Every mutation here writes its ``audit_log`` row inside the same transaction
as the state change (via ``log_audit``). The conservative-AI gate lives here:
an agent recommendation is persisted PENDING at ticket creation, and the ONLY
path to APPROVED is ``approve_recommendation`` — a human call that also writes
the audit row. No AI-authored state change happens without a human approve.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import log_audit
from app.core.permissions import Permission
from app.core.security import Actor
from app.db.models import AgentRecommendation, IntakeTicket, Person
from app.modules.intake.agents import (
    TicketContext,
    process_ticket_with_agent,
)
from app.modules.intake.agents.classifier import classify_intake
from app.modules.intake.schemas import CreateTicketRequest


class IntakeError(Exception):
    """Base for intake service errors → mapped to 4xx in the router."""


class NotFoundError(IntakeError):
    pass


class ConflictError(IntakeError):
    pass


class ForbiddenError(IntakeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _next_ticket_id(session: AsyncSession, organization_id: str) -> str:
    count = (
        await session.execute(
            select(func.count())
            .select_from(IntakeTicket)
            .where(IntakeTicket.organization_id == organization_id)
        )
    ).scalar_one()
    return f"REQ-{3501 + int(count)}"


async def _resolve_or_provision_requester(
    session: AsyncSession,
    *,
    actor: Actor,
    name: str,
    email: str | None,
) -> Person:
    """Resolve the requester Person, provisioning one if genuinely new.

    Intake legitimately receives filers from any business unit, so requester
    provisioning is an explicit, audited step of the intake flow — NOT a
    hidden FK-satisfying auto-create. The provisioning writes its own audit
    row so the new Person is attributable.
    """
    stmt = select(Person).where(
        Person.organization_id == actor.organization_id,
        Person.type == "EMPLOYEE",
    )
    if email:
        stmt = stmt.where(Person.email == email)
    else:
        stmt = stmt.where(Person.name == name)
    existing = (await session.execute(stmt)).scalars().first()
    if existing:
        return existing

    person = Person(
        organization_id=actor.organization_id,
        type="EMPLOYEE",
        name=name,
        email=email,
        payload_metadata={"provisioned_via": "intake"},
    )
    session.add(person)
    await session.flush()
    await log_audit(
        session,
        organization_id=actor.organization_id,
        actor_id=actor.user_id,
        actor_type="USER",
        action="person.provisioned",
        resource_type="Person",
        resource_id=person.id,
        after_json={"name": name, "email": email, "type": "EMPLOYEE"},
        metadata={"source": "intake"},
    )
    return person


async def create_ticket(
    session: AsyncSession, actor: Actor, req: CreateTicketRequest
) -> tuple[IntakeTicket, AgentRecommendation | None]:
    """Create a ticket, classify it deterministically, and attach the
    agent's PENDING recommendation. Audited end-to-end."""
    # 1. Deterministic classification (the spine).
    classification = classify_intake(req.description, hint_type=req.type_hint)

    # 2. Requester (explicit, audited provisioning if new).
    requester = await _resolve_or_provision_requester(
        session, actor=actor, name=req.requester_name, email=req.requester_email
    )

    # 3. Run the agent BEFORE opening the write transaction is unnecessary —
    #    we're already in the request session; classification + agent are
    #    read-only against the DB. The agent may call Claude (async).
    ticket_id = await _next_ticket_id(session, actor.organization_id)
    ctx = TicketContext(
        id=ticket_id,
        type=classification.type,
        description=req.description,
        requester_name=req.requester_name,
        department=req.department,
        category=classification.category,
    )
    agent, recommendation = await process_ticket_with_agent(ctx)

    # 4. Persist the ticket.
    ticket = IntakeTicket(
        id=ticket_id,
        organization_id=actor.organization_id,
        requester_id=requester.id,
        source=req.source,
        type=classification.type,
        priority=classification.priority,
        status="IN_REVIEW" if recommendation is not None else "AWAITING_TRIAGE",
        stage="triaged" if recommendation is not None else "new",
        description=req.description,
        department=req.department,
        assigned_to="Cockpit Queue",
        sla_hours=classification.sla_hours,
        sla_status="On Track",
        ai_triage_json={
            "category": classification.category,
            "matched": classification.matched,
            "agent_id": agent.id if agent else None,
        },
        agent_processed_at=_now() if agent else None,
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
            "source": ticket.source,
            "requester_id": requester.id,
        },
        metadata={"classifier_matched": classification.matched},
    )

    # 5. Persist the recommendation PENDING + audit as an AGENT actor.
    rec_row: AgentRecommendation | None = None
    if recommendation is not None:
        rec_row = AgentRecommendation(
            ticket_id=ticket.id,
            agent_id=recommendation.agent_id,
            confidence=recommendation.confidence,
            suggested_action=recommendation.suggested_action,
            drafted_response=recommendation.drafted_response,
            reasoning=recommendation.reasoning,
            concerns=recommendation.concerns,
            citations=recommendation.citations,
            short_form_reply=recommendation.short_form_reply,
            status="PENDING",
        )
        session.add(rec_row)
        await session.flush()
        await log_audit(
            session,
            organization_id=actor.organization_id,
            actor_id=None,
            actor_type="AGENT",
            action="intake.recommendation.generated",
            resource_type="AgentRecommendation",
            resource_id=rec_row.id,
            after_json={
                "agent_id": recommendation.agent_id,
                "suggested_action": recommendation.suggested_action,
                "confidence": recommendation.confidence,
                "degraded": recommendation.degraded,
                "status": "PENDING",
            },
            metadata={"ticket_id": ticket.id},
        )

    await session.commit()
    await session.refresh(ticket)
    if rec_row is not None:
        await session.refresh(rec_row)
    return ticket, rec_row


async def _load_ticket(session: AsyncSession, actor: Actor, ticket_id: str) -> IntakeTicket:
    ticket = await session.get(IntakeTicket, ticket_id)
    if ticket is None or ticket.organization_id != actor.organization_id:
        raise NotFoundError(f"Ticket {ticket_id} not found.")
    return ticket


async def _latest_pending_rec(
    session: AsyncSession, ticket_id: str
) -> AgentRecommendation | None:
    result = await session.execute(
        select(AgentRecommendation)
        .where(
            AgentRecommendation.ticket_id == ticket_id,
            AgentRecommendation.status == "PENDING",
        )
        .order_by(AgentRecommendation.created_at.desc())
    )
    return result.scalars().first()


async def approve_recommendation(
    session: AsyncSession,
    actor: Actor,
    ticket_id: str,
    *,
    edited_response: str | None = None,
) -> tuple[IntakeTicket, AgentRecommendation]:
    """The ONLY path from PENDING → APPROVED. Human-gated + audited."""
    ticket = await _load_ticket(session, actor, ticket_id)
    rec = await _latest_pending_rec(session, ticket_id)
    if rec is None:
        raise ConflictError("No pending recommendation to approve on this ticket.")

    before = {"rec_status": rec.status, "ticket_status": ticket.status}
    edited = bool(edited_response and edited_response.strip())
    if edited:
        rec.drafted_response = edited_response  # type: ignore[assignment]
        rec.status = "EDITED"
        action = "intake.recommendation.edited_approved"
    else:
        rec.status = "APPROVED"
        action = "intake.recommendation.approved"

    rec.reviewed_by = actor.user_id
    rec.reviewed_at = _now()

    ticket.status = "APPROVED"
    ticket.stage = "approved"
    ticket.triaged_by = actor.user_id
    ticket.triaged_at = _now()
    ticket.triaged_action = "approved"

    await log_audit(
        session,
        organization_id=actor.organization_id,
        actor_id=actor.user_id,
        actor_type="USER",
        action=action,
        resource_type="AgentRecommendation",
        resource_id=rec.id,
        before_json=before,
        after_json={"rec_status": rec.status, "ticket_status": ticket.status},
        metadata={"ticket_id": ticket.id, "edited": edited},
    )
    # NOTE: an approved workflow whose type resolves to matter-creation will
    # call @aegis/matter.create_matter here once the Matter module lands.
    await session.commit()
    await session.refresh(ticket)
    await session.refresh(rec)
    return ticket, rec


async def reject_recommendation(
    session: AsyncSession, actor: Actor, ticket_id: str, *, reason: str
) -> tuple[IntakeTicket, AgentRecommendation]:
    ticket = await _load_ticket(session, actor, ticket_id)
    rec = await _latest_pending_rec(session, ticket_id)
    if rec is None:
        raise ConflictError("No pending recommendation to reject on this ticket.")

    before = {"rec_status": rec.status, "ticket_status": ticket.status}
    rec.status = "REJECTED"
    rec.reviewed_by = actor.user_id
    rec.reviewed_at = _now()

    ticket.status = "REJECTED"
    ticket.stage = "triaged"
    ticket.triaged_by = actor.user_id
    ticket.triaged_at = _now()
    ticket.triaged_action = "rejected"

    await log_audit(
        session,
        organization_id=actor.organization_id,
        actor_id=actor.user_id,
        actor_type="USER",
        action="intake.recommendation.rejected",
        resource_type="AgentRecommendation",
        resource_id=rec.id,
        before_json=before,
        after_json={"rec_status": "REJECTED", "ticket_status": "REJECTED"},
        metadata={"ticket_id": ticket.id, "reason": reason},
    )
    await session.commit()
    await session.refresh(ticket)
    await session.refresh(rec)
    return ticket, rec


async def list_tickets(session: AsyncSession, actor: Actor) -> list[IntakeTicket]:
    """Scope-aware read. read_all → whole org; else read_own → own filings."""
    stmt = select(IntakeTicket).where(
        IntakeTicket.organization_id == actor.organization_id
    )
    if Permission.INTAKE_READ_ALL_TICKETS.value not in actor.permissions:
        if Permission.INTAKE_READ_OWN_TICKETS.value not in actor.permissions:
            raise ForbiddenError("Not permitted to read intake tickets.")
        # Own filings: tickets whose requester Person is linked to this user.
        own_person_ids = (
            await session.execute(
                select(Person.id).where(
                    Person.organization_id == actor.organization_id,
                    Person.user_id == actor.user_id,
                )
            )
        ).scalars().all()
        stmt = stmt.where(IntakeTicket.requester_id.in_(own_person_ids or ["__none__"]))
    stmt = stmt.order_by(IntakeTicket.submitted_at.desc())
    return list((await session.execute(stmt)).scalars().all())


async def get_ticket_with_recommendation(
    session: AsyncSession, actor: Actor, ticket_id: str
) -> tuple[IntakeTicket, AgentRecommendation | None]:
    ticket = await _load_ticket(session, actor, ticket_id)
    result = await session.execute(
        select(AgentRecommendation)
        .where(AgentRecommendation.ticket_id == ticket_id)
        .order_by(AgentRecommendation.created_at.desc())
    )
    rec = result.scalars().first()
    return ticket, rec
