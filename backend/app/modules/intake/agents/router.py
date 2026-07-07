"""Agent registry + router.

The router picks the FIRST agent (in specificity order) whose ``can_handle``
matches the ticket; if none match, the ticket falls through to honest human
triage (returns ``None``). This is deterministic — no model decides routing.

Only the NDA agent ships in this first backend increment; the remaining
five agents (contract review, vendor screening, policy Q&A, FAQ, trademark)
land as the module expands, each registered here in specificity order.
"""

from __future__ import annotations

import logging

from app.modules.intake.agents.base import Agent, Recommendation, TicketContext, build_rec
from app.modules.intake.agents.nda import NDAAgent

logger = logging.getLogger("aegis.intake.agents")

# Specificity order — most specific first. Expanded as agents are added.
_ORDER: list[Agent] = [NDAAgent()]

ALL_AGENTS: list[Agent] = list(_ORDER)
AGENTS_BY_ID: dict[str, Agent] = {a.id: a for a in ALL_AGENTS}


def route_to_agent(ticket: TicketContext) -> Agent | None:
    for agent in ALL_AGENTS:
        try:
            if agent.can_handle(ticket):
                return agent
        except Exception:  # noqa: BLE001 — a broken matcher never blocks intake
            logger.exception("agent %s can_handle raised", agent.id)
    return None


async def process_ticket_with_agent(
    ticket: TicketContext,
) -> tuple[Agent | None, Recommendation | None]:
    """Route and run. Returns (agent, recommendation) or (None, None).

    An agent exception yields a visible low-confidence, human-review
    recommendation rather than a silent failure — the ticket never vanishes.
    """
    agent = route_to_agent(ticket)
    if agent is None:
        return None, None
    try:
        rec = await agent.process(ticket)
        return agent, rec
    except Exception as exc:  # noqa: BLE001
        logger.exception("agent %s process failed", agent.id)
        return agent, build_rec(
            agent.id,
            confidence=0.25,
            suggested_action="flag-for-review",
            drafted_response="",
            reasoning=f"Agent {agent.name} encountered an error. Manual triage recommended.",
            concerns=[f"Agent error: {type(exc).__name__}."],
            degraded=True,
        )
