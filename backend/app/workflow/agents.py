"""Workflow-agent handler registry.

A ladder step declared ``kind='agent'`` names a handler here via
``agent_config.agent_key``. When the ladder arrives at that step the engine
runs the handler and persists its output as a PENDING ``AgentDecision`` —
the handler itself NEVER moves the ladder; the human approval in the Cockpit
executes the proposed action (conservative-AI rule #1).

Handler contract (async; deterministic handlers ignore ``deps``, ontology-
aware ones read through the SAME GraphRAG/ontology surface as humans — no
agent has a private data path; LLM-backed handlers go through
``core/ai.py``)::

    @register_workflow_agent("nda_reviewer")
    async def nda_reviewer(
        context: dict, step_config: dict, deps: WorkflowAgentDeps
    ) -> WorkflowAgentOutput: ...

``WorkflowAgentOutput`` fields:
  proposed_action   "approve" | "send_back" | "reject"
  target_step       int | None (required for send_back)
  comment           human-readable findings, shown in the Cockpit + audit
  confidence        0.0–1.0 (low confidence surfaces prominently; it never
                    auto-clears anything either way)
  drafted_response  optional draft (memo/reply) the approver may edit
  citations         [{type, id, title}] — the ontology objects the agent
                    relied on; clickable in the Cockpit
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class WorkflowAgentDeps:
    """What an agent may read through — the shared surfaces only."""

    session: AsyncSession
    organization_id: str


@dataclass
class WorkflowAgentOutput:
    proposed_action: str
    comment: str
    confidence: float
    target_step: int | None = None
    drafted_response: str = ""
    citations: list[dict] = field(default_factory=list)
    # Edge specs applied ONLY when a human approves the decision — the
    # generic executor writes them post-approval in the same transaction.
    # Shape: {src_type, src_id, label, dst_type, dst_id, properties}.
    # This is how e.g. the screening result lands on the graph: the human
    # approving the screening IS the authorization for the write.
    ontology_writes: list[dict] = field(default_factory=list)


WorkflowAgentHandler = Callable[
    [dict, dict, WorkflowAgentDeps], Awaitable[WorkflowAgentOutput]
]

_HANDLERS: dict[str, WorkflowAgentHandler] = {}


def register_workflow_agent(key: str) -> Callable[[WorkflowAgentHandler], WorkflowAgentHandler]:
    def deco(fn: WorkflowAgentHandler) -> WorkflowAgentHandler:
        if key in _HANDLERS:
            raise RuntimeError(f"Workflow agent '{key}' registered twice.")
        _HANDLERS[key] = fn
        return fn

    return deco


def get_workflow_agent(key: str) -> WorkflowAgentHandler | None:
    return _HANDLERS.get(key)


def registered_workflow_agents() -> tuple[str, ...]:
    return tuple(_HANDLERS.keys())
