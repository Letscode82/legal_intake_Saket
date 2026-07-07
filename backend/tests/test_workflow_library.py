"""Governance-workflow library — pure unit tests (no database).

Covers the deterministic classify() triage routes, the structural
invariants of every ladder in WORKFLOW_LIBRARY, request-type routing
integrity, and the registered library agent handlers.
"""

from __future__ import annotations

import pytest

import asyncio

import app.modules.contracts.agents  # noqa: F401 — nda_reviewer + contract_risk_reviewer v2
import app.modules.matter.agents  # noqa: F401 — trademark_clearance_reviewer
import app.modules.spend.agents  # noqa: F401 — counterparty_screener v2
from app.workflow.agents import get_workflow_agent
from app.modules.spend.agents.vendor import counterparty_screener
from app.workflow.library import (
    REQUEST_TYPES,
    WORKFLOW_LIBRARY,
    classify,
)


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("we received a para iv notice for product X", "litigation"),
        ("need an NDA with Acme", "nda"),
        ("vendor onboarding for a new supplier", "vendor"),
        ("usfda warning letter received", "regulatory"),
        ("personal data breach reported", "data_breach"),
    ],
)
def test_classify_routes(description, expected):
    request_type, confidence = classify(description)
    assert request_type == expected
    assert 0.6 <= confidence <= 0.95


def test_classify_default_lane_low_confidence():
    assert classify("xyzzy plugh qwertyuiop") == ("contract", 0.4)


def test_every_request_type_maps_to_a_ladder():
    library_keys = {d["key"] for d in WORKFLOW_LIBRARY}
    for request_type, spec in REQUEST_TYPES.items():
        assert spec["definition_key"] in library_keys, (
            f"REQUEST_TYPES[{request_type!r}] points at missing ladder "
            f"{spec['definition_key']!r}"
        )


def test_every_ladder_has_contiguous_steps():
    for definition in WORKFLOW_LIBRARY:
        steps = definition["steps"]
        assert 1 <= len(steps) <= 15, definition["key"]
        orders = [s["step_order"] for s in steps]
        assert orders == list(range(1, len(steps) + 1)), definition["key"]


def test_every_agent_step_resolves_to_a_registered_handler():
    # Importing app.workflow.library registers all library handlers.
    for definition in WORKFLOW_LIBRARY:
        for step in definition["steps"]:
            if step["kind"] != "agent":
                continue
            agent_key = step["agent_config"]["agent_key"]
            assert get_workflow_agent(agent_key) is not None, (
                f"{definition['key']} step {step['step_order']} names "
                f"unregistered agent {agent_key!r}"
            )


def test_counterparty_screener_sends_back_on_sanctions_hit():
    # Deterministic handlers ignore deps — None is fine for a direct call.
    output = asyncio.run(counterparty_screener({"sanctions_hit": True}, {}, None))
    assert output.proposed_action == "send_back"
    assert output.target_step == 1
