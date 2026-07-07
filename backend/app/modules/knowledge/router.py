"""Knowledge Q&A HTTP surface — the FAQ and Policy agents.

Gated on ``get_current_actor`` only: any authenticated user may ask. The
FAQ agent is THE direct-answer agent — permissible because it can only
quote approved ``KnowledgeEntry`` content (and hands off to the Front Door
whenever it can't). No mutation beyond the AnswerRecord (Event + CITES
edge), so no governance gate applies.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Actor, get_current_actor
from app.db.session import get_session
from app.modules.knowledge import service

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=2000)


class KnowledgeAnswerOut(BaseModel):
    answered: bool
    # Always carries the framing note ("general guidance … not legal
    # advice") when answered; the conflict message when conflict=True.
    answer: str | None = None
    conflict: bool = False
    # {"hint": "file via /api/v1/intake/front-door", ...} when not answered.
    handoff: dict | None = None
    # [{type, id, slug, title, version, (policy_name, effective_date)}]
    citations: list[dict] = []


@router.post(
    "/ask",
    response_model=KnowledgeAnswerOut,
    summary="Ask the FAQ agent — quotes approved knowledge or hands off "
    "to the Legal Front Door.",
)
async def ask_faq(
    body: AskIn,
    session: AsyncSession = Depends(get_session),
    actor: Actor = Depends(get_current_actor),
) -> KnowledgeAnswerOut:
    result = await service.answer_faq(session, actor, body.question)
    return KnowledgeAnswerOut(**result)


@router.post(
    "/policy-ask",
    response_model=KnowledgeAnswerOut,
    summary="Ask the Policy agent — quotes the governing policy verbatim, "
    "or routes conflicts to the policy owner.",
)
async def ask_policy(
    body: AskIn,
    session: AsyncSession = Depends(get_session),
    actor: Actor = Depends(get_current_actor),
) -> KnowledgeAnswerOut:
    result = await service.answer_policy(session, actor, body.question)
    return KnowledgeAnswerOut(**result)
