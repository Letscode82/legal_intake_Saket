"""Workflow-agent handler registry.

A ladder step declared ``kind='agent'`` names a handler here via
``agent_config.agent_key``. When the ladder arrives at that step the engine
runs the handler and persists its output as a PENDING ``AgentDecision`` —
the handler itself NEVER moves the ladder; the human approval in the Cockpit
executes the proposed action (conservative-AI rule #1).

Handler contract (sync, pure over the instance context — deterministic
defaults; LLM-backed handlers swap in per agent PR and go through
``core/ai.py``)::

    @register_workflow_agent("nda_reviewer")
    def nda_reviewer(context: dict, step_config: dict) -> WorkflowAgentOutput: ...

``WorkflowAgentOutput`` fields:
  proposed_action  "approve" | "send_back" | "reject"
  target_step      int | None (required for send_back)
  comment          human-readable findings, shown in the Cockpit + audit
  confidence       0.0–1.0 (low confidence surfaces prominently; it never
                   auto-clears anything either way)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class WorkflowAgentOutput:
    proposed_action: str
    comment: str
    confidence: float
    target_step: int | None = None


WorkflowAgentHandler = Callable[[dict, dict], WorkflowAgentOutput]

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
