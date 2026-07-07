"""Agent recommendation shape + the conservative-AI safety invariant.

An agent NEVER mutates state. It produces a ``Recommendation`` — the AI's
proposal. A human must approve it (via the gated service call) before
anything is sent or a matter is spawned. This is the product, not a feature.

The degraded-recommendation helper is the single chokepoint every agent's
Claude-failure path routes through: when the model is unavailable, the agent
may still surface a template draft, but it is ALWAYS demoted to low
confidence + ``flag-for-review`` and NEVER recommends auto-send. The
invariant can't drift per-agent because it lives here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class TicketContext:
    """The subset of ticket state an agent reasons over.

    Constructed from the persisted ticket. Free-text fields (description,
    requester name, extracted document text) are UNTRUSTED input.
    """

    id: str
    type: str
    description: str
    requester_name: str
    department: str | None = None
    category: str | None = None


@dataclass
class Recommendation:
    agent_id: str
    confidence: float
    suggested_action: str
    drafted_response: str
    reasoning: str
    concerns: list[str] = field(default_factory=list)
    citations: list[dict] = field(default_factory=list)
    short_form_reply: str | None = None
    # True when produced without a live model call (degraded/template path).
    degraded: bool = False


DEGRADED_CONFIDENCE = 0.4
DEGRADED_ACTION = "flag-for-review"
_DEGRADED_LEAD_CONCERN = (
    "⚠ AI review unavailable — this is a template draft, not an AI-generated "
    "recommendation. Attorney review required before sending."
)


def build_rec(
    agent_id: str,
    *,
    confidence: float,
    suggested_action: str,
    drafted_response: str,
    reasoning: str,
    concerns: list[str] | None = None,
    citations: list[dict] | None = None,
    short_form_reply: str | None = None,
    degraded: bool = False,
) -> Recommendation:
    return Recommendation(
        agent_id=agent_id,
        confidence=confidence,
        suggested_action=suggested_action,
        drafted_response=drafted_response,
        reasoning=reasoning,
        concerns=concerns or [],
        citations=citations or [],
        short_form_reply=short_form_reply,
        degraded=degraded,
    )


def build_degraded_rec(
    agent_id: str,
    *,
    drafted_response: str,
    reasoning: str,
    concerns: list[str] | None = None,
    citations: list[dict] | None = None,
    short_form_reply: str | None = None,
) -> Recommendation:
    """Force the safety invariant regardless of the agent's happy-path values."""
    return build_rec(
        agent_id,
        confidence=DEGRADED_CONFIDENCE,
        suggested_action=DEGRADED_ACTION,
        drafted_response=drafted_response,
        reasoning=reasoning,
        concerns=[_DEGRADED_LEAD_CONCERN, *(concerns or [])],
        citations=citations,
        short_form_reply=short_form_reply,
        degraded=True,
    )


class Agent(Protocol):
    id: str
    name: str
    production_ready: bool

    def can_handle(self, ticket: TicketContext) -> bool: ...

    async def process(self, ticket: TicketContext) -> Recommendation: ...
