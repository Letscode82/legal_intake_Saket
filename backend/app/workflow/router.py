"""Workflow HTTP surface — instance state, RAG progress, human actions.

Human ladder actions are permission-gated: approve/send_back require
``intake:approve_recommendation``, reject requires
``intake:reject_recommendation``, cancel requires ``intake:close_ticket``
(every ladder today drives an intake-originated matter; per-module gating
generalizes when other modules start ladders). Agent-step proposals are NOT
actioned here — they are approved in the Cockpit (`/cockpit/decisions`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission
from app.core.security import Actor, get_current_actor
from app.db.session import get_session
from app.workflow import engine, service

router = APIRouter(prefix="/workflow", tags=["workflow"])


class StepRagOut(BaseModel):
    step_order: int
    name: str
    screen_key: str
    color: str
    overdue: bool
    kind: str


class TransitionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_step_order: int
    to_step_order: int | None
    action: str
    actor_id: str | None
    actor_label: str
    comment: str | None
    created_at: datetime


class InstanceOut(BaseModel):
    id: str
    definition_key: str
    definition_name: str
    definition_version: int
    entity_type: str
    entity_id: str
    status: str
    current_step_order: int
    version: int
    context: dict
    rag: list[StepRagOut]
    transitions: list[TransitionOut]


class ActionIn(BaseModel):
    action: Literal["approve", "reject", "send_back", "cancel"]
    comment: str | None = Field(default=None, max_length=2000)
    target_step: int | None = None
    expected_version: int | None = None


_ACTION_PERMISSION: dict[str, Permission] = {
    "approve": Permission.INTAKE_APPROVE_RECOMMENDATION,
    "send_back": Permission.INTAKE_APPROVE_RECOMMENDATION,
    "reject": Permission.INTAKE_REJECT_RECOMMENDATION,
    "cancel": Permission.INTAKE_CLOSE_TICKET,
}


def _instance_out(instance) -> InstanceOut:
    return InstanceOut(
        id=instance.id,
        definition_key=instance.definition.key,
        definition_name=instance.definition.name,
        definition_version=instance.definition.version,
        entity_type=instance.entity_type,
        entity_id=instance.entity_id,
        status=instance.status,
        current_step_order=instance.current_step_order,
        version=instance.version,
        context=instance.context or {},
        rag=[StepRagOut(**s) for s in engine.rag_status(instance)],
        transitions=[TransitionOut.model_validate(t) for t in instance.transitions],
    )


async def _load(session: AsyncSession, actor: Actor, instance_id: str):
    instance = await service.get_instance(session, actor.organization_id, instance_id)
    if instance is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow instance {instance_id} not found.",
        )
    return instance


@router.get(
    "/instances/{instance_id}",
    response_model=InstanceOut,
    summary="Instance state + per-step RAG progress + transition timeline.",
)
async def get_instance(
    instance_id: str,
    session: AsyncSession = Depends(get_session),
    actor: Actor = Depends(get_current_actor),
) -> InstanceOut:
    if (
        Permission.INTAKE_READ_ALL_TICKETS.value not in actor.permissions
        and Permission.INTAKE_READ_OWN_TICKETS.value not in actor.permissions
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted.")
    instance = await _load(session, actor, instance_id)
    return _instance_out(instance)


@router.post(
    "/instances/{instance_id}/actions",
    response_model=InstanceOut,
    summary="Human ladder action: approve / reject / send_back / cancel.",
)
async def act_on_instance(
    instance_id: str,
    body: ActionIn,
    session: AsyncSession = Depends(get_session),
    actor: Actor = Depends(get_current_actor),
) -> InstanceOut:
    needed = _ACTION_PERMISSION[body.action]
    if needed.value not in actor.permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission {needed.value}.",
        )
    instance = await _load(session, actor, instance_id)
    try:
        await engine.act(
            session,
            instance,
            action=body.action,
            actor_id=actor.user_id,
            actor_label=actor.name,
            comment=body.comment,
            target_step=body.target_step,
            expected_version=body.expected_version,
        )
    except engine.WorkflowConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except engine.WorkflowValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    await session.commit()
    await session.refresh(instance)
    return _instance_out(instance)
