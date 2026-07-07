"""Cockpit decisions HTTP surface — the human approval queue.

Thin HTTP layer over ``app.core.governance``: list/read the org's
``AgentDecision`` rows and expose the ONLY two paths out of PENDING
(approve — which executes the governed action — and reject). All error
semantics live in the governance service; this router only maps them to
status codes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import governance
from app.core.permissions import Permission
from app.core.security import Actor, require_permission
from app.db.models import AgentDecision
from app.db.session import get_session

router = APIRouter(prefix="/cockpit", tags=["cockpit"])

DecisionStatus = Literal["PENDING", "APPROVED", "APPROVED_WITH_OVERRIDE", "REJECTED"]


class DecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    resource_type: str
    resource_id: str
    action_key: str
    action_payload: dict
    recommendation: dict
    status: str
    decided_by: str | None
    decided_at: datetime | None
    decision_comment: str | None
    executed_audit_id: str | None
    created_at: datetime


class ApproveDecisionRequest(BaseModel):
    payload_override: dict | None = None
    comment: str | None = Field(default=None, max_length=2000)


class RejectDecisionRequest(BaseModel):
    comment: str = Field(min_length=1, max_length=2000)


# Read gate: the Cockpit queue is a triage-read surface, so it rides
# intake:read_all_tickets for now — every decision in the queue today is an
# intake-side recommendation. Per-module gating (matter:read_all for matter
# decisions, etc.) lands when non-intake decisions exist.
_READ_GATE = require_permission(Permission.INTAKE_READ_ALL_TICKETS)


@router.get(
    "/decisions",
    response_model=list[DecisionOut],
    summary="List the organization's agent decisions (newest first).",
)
async def list_decisions(
    status_filter: DecisionStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50),
    session: AsyncSession = Depends(get_session),
    actor: Actor = Depends(_READ_GATE),
) -> list[DecisionOut]:
    query = (
        select(AgentDecision)
        .where(AgentDecision.organization_id == actor.organization_id)
        .order_by(AgentDecision.created_at.desc())
        .limit(min(max(limit, 1), 200))
    )
    if status_filter is not None:
        query = query.where(AgentDecision.status == status_filter)
    rows = (await session.execute(query)).scalars().all()
    return [DecisionOut.model_validate(r) for r in rows]


@router.get(
    "/decisions/{decision_id}",
    response_model=DecisionOut,
    summary="Get a single agent decision.",
)
async def get_decision(
    decision_id: str,
    session: AsyncSession = Depends(get_session),
    actor: Actor = Depends(_READ_GATE),
) -> DecisionOut:
    decision = await session.get(AgentDecision, decision_id)
    if decision is None or decision.organization_id != actor.organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AgentDecision {decision_id} not found.",
        )
    return DecisionOut.model_validate(decision)


@router.post(
    "/decisions/{decision_id}/approve",
    response_model=DecisionOut,
    summary="Human approval — executes the governed action in the same "
    "transaction as the status flip and the audit row.",
)
async def approve(
    decision_id: str,
    body: ApproveDecisionRequest,
    session: AsyncSession = Depends(get_session),
    actor: Actor = Depends(
        require_permission(Permission.INTAKE_APPROVE_RECOMMENDATION)
    ),
) -> DecisionOut:
    try:
        decision = await governance.approve_decision(
            session,
            actor,
            decision_id,
            payload_override=body.payload_override,
            comment=body.comment,
        )
    except governance.DecisionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except governance.DecisionNotPendingError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except governance.UnknownActionError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    except governance.GovernanceError as exc:
        # Executor refused (e.g. the proposal went stale because the target
        # moved on). The transaction rolled back; the decision stays PENDING.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    # Any other executor exception propagates as a 500 — an approval that
    # cannot execute must fail loud (the transaction rolled back; the
    # decision stays PENDING).
    return DecisionOut.model_validate(decision)


@router.post(
    "/decisions/{decision_id}/reject",
    response_model=DecisionOut,
    summary="Human rejection of a pending agent decision (audited).",
)
async def reject(
    decision_id: str,
    body: RejectDecisionRequest,
    session: AsyncSession = Depends(get_session),
    actor: Actor = Depends(
        require_permission(Permission.INTAKE_REJECT_RECOMMENDATION)
    ),
) -> DecisionOut:
    try:
        decision = await governance.reject_decision(
            session, actor, decision_id, comment=body.comment
        )
    except governance.DecisionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except governance.DecisionNotPendingError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except governance.UnknownActionError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    return DecisionOut.model_validate(decision)
