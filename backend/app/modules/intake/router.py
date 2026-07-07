"""Legal Intake HTTP surface. Every mutation is permission-gated."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
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
