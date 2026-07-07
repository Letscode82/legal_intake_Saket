"""Legal Intake HTTP surface. Every mutation is permission-gated."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission
from app.core.security import Actor, get_current_actor, require_permission
from app.db.session import get_session
from app.modules.intake import service
from app.modules.intake.schemas import (
    ApproveRequest,
    CreateTicketRequest,
    RecommendationOut,
    RejectRequest,
    TicketOut,
    TicketWithRecommendation,
)

router = APIRouter(prefix="/intake", tags=["intake"])


def _ticket_out(ticket) -> TicketOut:
    return TicketOut.model_validate(ticket)


def _rec_out(rec) -> RecommendationOut | None:
    return RecommendationOut.model_validate(rec) if rec is not None else None


@router.post(
    "/tickets",
    response_model=TicketWithRecommendation,
    status_code=status.HTTP_201_CREATED,
    summary="File an intake ticket (classified + agent-triaged, PENDING).",
)
async def create_ticket(
    body: CreateTicketRequest,
    session: AsyncSession = Depends(get_session),
    actor: Actor = Depends(require_permission(Permission.INTAKE_CREATE_TICKET)),
) -> TicketWithRecommendation:
    ticket, rec = await service.create_ticket(session, actor, body)
    return TicketWithRecommendation(ticket=_ticket_out(ticket), recommendation=_rec_out(rec))


class FrontDoorRequest(CreateTicketRequest):
    # Explicit lane (see GET /intake/front-door/types); omitted → the
    # deterministic classifier triages the description.
    request_type: str | None = None
    # Inbound-channel dedup key (email internetMessageId, webhook messageId).
    external_message_id: str | None = None
    # Extra ladder context evaluated by skip_if rules and agent handlers
    # (e.g. contract_value, sanctions_hit, records_affected).
    context: dict | None = None


class FrontDoorOut(BaseModel):
    ticket: TicketOut
    workflow_instance_id: str
    request_type: str
    routing_confidence: float
    deduplicated: bool
    current_step: int
    workflow_status: str


@router.get(
    "/front-door/types",
    summary="The Front Door catalog: request types and the ladder each routes to.",
)
async def front_door_types(
    actor: Actor = Depends(get_current_actor),
) -> list[dict]:
    from app.workflow.library import REQUEST_TYPES

    return [{"request_type": k, **v} for k, v in REQUEST_TYPES.items()]


@router.post(
    "/front-door",
    response_model=FrontDoorOut,
    status_code=status.HTTP_201_CREATED,
    summary="One door for everything reaching Legal — routes to the "
    "matter-type governance ladder; agent steps land as PENDING decisions.",
)
async def front_door(
    body: FrontDoorRequest,
    session: AsyncSession = Depends(get_session),
    actor: Actor = Depends(require_permission(Permission.INTAKE_CREATE_TICKET)),
) -> FrontDoorOut:
    from app.modules.intake import front_door as fd

    try:
        ticket, instance, lane, confidence, deduplicated = (
            await fd.submit_front_door_request(
                session,
                actor,
                description=body.description,
                requester_name=body.requester_name,
                requester_email=body.requester_email,
                department=body.department,
                source=body.source,
                request_type=body.request_type,
                external_message_id=body.external_message_id,
                context=body.context,
            )
        )
    except fd.UnknownRequestTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return FrontDoorOut(
        ticket=_ticket_out(ticket),
        workflow_instance_id=instance.id,
        request_type=lane,
        routing_confidence=confidence,
        deduplicated=deduplicated,
        current_step=instance.current_step_order,
        workflow_status=instance.status,
    )


@router.get("/tickets", response_model=list[TicketOut], summary="List tickets (scope-aware).")
async def list_tickets(
    session: AsyncSession = Depends(get_session),
    actor: Actor = Depends(get_current_actor),
) -> list[TicketOut]:
    try:
        tickets = await service.list_tickets(session, actor)
    except service.ForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return [_ticket_out(t) for t in tickets]


@router.get(
    "/tickets/{ticket_id}",
    response_model=TicketWithRecommendation,
    summary="Get a ticket + its latest recommendation.",
)
async def get_ticket(
    ticket_id: str,
    session: AsyncSession = Depends(get_session),
    actor: Actor = Depends(get_current_actor),
) -> TicketWithRecommendation:
    if (
        Permission.INTAKE_READ_ALL_TICKETS.value not in actor.permissions
        and Permission.INTAKE_READ_OWN_TICKETS.value not in actor.permissions
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted.")
    try:
        ticket, rec = await service.get_ticket_with_recommendation(session, actor, ticket_id)
    except service.NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return TicketWithRecommendation(ticket=_ticket_out(ticket), recommendation=_rec_out(rec))


@router.post(
    "/tickets/{ticket_id}/approve",
    response_model=TicketWithRecommendation,
    summary="Human approval — the ONLY path from PENDING to APPROVED.",
)
async def approve(
    ticket_id: str,
    body: ApproveRequest,
    session: AsyncSession = Depends(get_session),
    actor: Actor = Depends(require_permission(Permission.INTAKE_APPROVE_RECOMMENDATION)),
) -> TicketWithRecommendation:
    try:
        ticket, rec = await service.approve_recommendation(
            session, actor, ticket_id, edited_response=body.edited_response
        )
    except service.NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except service.ConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return TicketWithRecommendation(ticket=_ticket_out(ticket), recommendation=_rec_out(rec))


@router.post(
    "/tickets/{ticket_id}/reject",
    response_model=TicketWithRecommendation,
    summary="Human rejection of a pending recommendation.",
)
async def reject(
    ticket_id: str,
    body: RejectRequest,
    session: AsyncSession = Depends(get_session),
    actor: Actor = Depends(require_permission(Permission.INTAKE_REJECT_RECOMMENDATION)),
) -> TicketWithRecommendation:
    try:
        ticket, rec = await service.reject_recommendation(
            session, actor, ticket_id, reason=body.reason
        )
    except service.NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except service.ConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return TicketWithRecommendation(ticket=_ticket_out(ticket), recommendation=_rec_out(rec))
