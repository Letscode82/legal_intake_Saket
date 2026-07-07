"""Vendor / Sanctions Screening Agent v2 — list-backed, cited, human-gated
(Agent 2 in the doc; ladder step 2 of ``vendor_onboarding``).

Screening is DETERMINISTIC — no Claude call (conservative-AI rule #2: an
LLM adds nothing to a list lookup except non-reproducibility). The handler
fuzzy-matches the vendor's legal name against the curated ``SanctionsEntry``
table (a global reference list, not org data) and applies the playbook:

  score above the hit threshold   → send_back to intake (blocked until
                                     compliance clears it)
  score in the ambiguity band     → approve proposal at confidence 0.5 —
                                     deliberately BELOW the step's 0.85
                                     min_confidence so the Cockpit surfaces
                                     the below-threshold concern. The agent
                                     NEVER auto-clears ambiguity; a human
                                     resolves every partial match.
  score below the ambiguity floor → clear, but point-in-time only: lists
                                     change daily; the re-screen cadence is
                                     the ongoing control, not this result.

The handler only ever RECOMMENDS: its output parks as a PENDING
AgentDecision. The SCREENED_ON edge proposed via ``ontology_writes`` is
applied by the workflow engine ONLY when a human approves the decision —
the approval IS the authorization for the write; this handler never touches
the graph itself. Every downstream agent (contract review, invoice
approval, …) reads the screening flag from the latest SCREENED_ON edge on
the Counterparty node — the one-brain surface, never a private column.

All free text in ``context`` (request description, vendor name) is
UNTRUSTED input — it is matched with regex/difflib as data, never
interpreted as instructions.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from sqlalchemy import select

from app.db.models import Counterparty, SanctionsEntry
from app.workflow.agents import (
    WorkflowAgentDeps,
    WorkflowAgentOutput,
    register_workflow_agent,
)

# The demo list identity the SCREENED_ON edge points at; the OFAC SDN feed
# loader (PR 10 follow-up job) upserts into the same SanctionsEntry table.
_LIST_ID = "OFAC_SDN_DEMO"

# A score STRICTLY above the hit threshold is a hit; a score exactly at the
# boundary is still a partial match — ties park for human review (the
# conservative reading: ambiguity never auto-clears, and it never
# auto-blocks on a coin-flip either).
_HIT_THRESHOLD = 0.85
_AMBIGUITY_FLOOR = 0.65

_VENDOR_RE = re.compile(
    r"(?:vendor|supplier|counterparty|with|for)\s+"
    r"([A-Z][A-Za-z0-9&.\- ]{2,60}?)"
    r"(?=[,.;:\n]|\s+(?:re\b|regarding|for|by|about)|$)"
)


def _extract_vendor_name(context: dict) -> str | None:
    for key in ("vendor_name", "counterparty"):
        explicit = context.get(key)
        if explicit:
            return str(explicit)[:120]
    match = _VENDOR_RE.search(context.get("description", "") or "")
    return match.group(1).strip() if match else None


async def _best_list_match(
    deps: WorkflowAgentDeps, vendor_name: str
) -> tuple[float, SanctionsEntry | None, str | None]:
    """Fuzzy-match the vendor name against every list entry + alias.

    Returns (best_score, best_entry, matched_candidate_name). The
    SanctionsEntry table is a global reference list — no org filter.
    """
    entries = (await deps.session.execute(select(SanctionsEntry))).scalars().all()
    query = vendor_name.strip().lower()
    best_score, best_entry, best_name = 0.0, None, None
    for entry in entries:
        for candidate in [entry.name, *(entry.aliases or [])]:
            score = SequenceMatcher(None, query, str(candidate).lower()).ratio()
            if score > best_score:
                best_score, best_entry, best_name = score, entry, str(candidate)
    return best_score, best_entry, best_name


async def _resolve_counterparty(
    deps: WorkflowAgentDeps, vendor_name: str
) -> Counterparty | None:
    return (
        await deps.session.execute(
            select(Counterparty).where(
                Counterparty.organization_id == deps.organization_id,
                Counterparty.name.ilike(f"%{vendor_name}%"),
            )
        )
    ).scalars().first()


@register_workflow_agent("counterparty_screener")
async def counterparty_screener(
    context: dict, step_config: dict, deps: WorkflowAgentDeps
) -> WorkflowAgentOutput:
    """Sanctions / debarment screening for vendor onboarding.

    Never auto-clears ambiguity — partial matches surface at confidence 0.5
    for a human to resolve. The proposed SCREENED_ON edge in
    ``ontology_writes`` is applied only when a human approves the decision;
    other agents read the screening flag from the latest SCREENED_ON edge
    on the Counterparty node.
    """
    # Back-compat shortcut: existing E2E fixtures assert the flag-driven
    # path (context carries the screening verdict directly). It wins before
    # any list lookup so those ladders behave exactly as before.
    if context.get("sanctions_hit") or context.get("debarred"):
        return WorkflowAgentOutput(
            proposed_action="send_back",
            target_step=1,
            confidence=0.95,
            comment=(
                "Agent: screening HIT (sanctions/debarment list) — "
                "onboarding returned; obtain clarification."
            ),
        )

    vendor_name = _extract_vendor_name(context)
    if not vendor_name:
        return WorkflowAgentOutput(
            proposed_action="approve",
            confidence=0.4,
            comment=(
                "Agent: no vendor name could be extracted from the request "
                "— screening requires a legal-entity name; ask the requester "
                "before clearing."
            ),
        )

    score, entry, matched_name = await _best_list_match(deps, vendor_name)
    cp = await _resolve_counterparty(deps, vendor_name)

    citations: list[dict] = []
    if cp is not None:
        citations.append({"type": "Counterparty", "id": cp.id, "title": cp.name})

    list_source = entry.list_source if entry is not None else _LIST_ID
    list_version = entry.list_version if entry is not None else "unknown"

    if entry is not None and score > _HIT_THRESHOLD:
        result = "hit"
        citations.append(
            {"type": "SanctionsEntry", "id": entry.id, "title": entry.name}
        )
        proposed_action, target_step, confidence = "send_back", 1, 0.95
        comment = (
            f"Agent: screening HIT — '{vendor_name}' matched list entry "
            f"'{entry.name}' (matched name '{matched_name}', program "
            f"{entry.program}, {list_source} version {list_version}, score "
            f"{round(score, 3)}). Onboarding is returned to intake and "
            "blocks until compliance clears it."
        )
    elif entry is not None and score >= _AMBIGUITY_FLOOR:
        result = "ambiguous"
        citations.append(
            {"type": "SanctionsEntry", "id": entry.id, "title": entry.name}
        )
        proposed_action, target_step, confidence = "approve", None, 0.5
        comment = (
            f"Agent: partial match — '{vendor_name}' scored "
            f"{round(score, 3)} against list entry '{entry.name}' (matched "
            f"name '{matched_name}', {list_source} version {list_version}); "
            "requires human review — ambiguity never auto-clears."
        )
    else:
        result = "clear"
        matched_name = None
        proposed_action, target_step, confidence = "approve", None, 0.9
        comment = (
            f"Agent: screening CLEAR — '{vendor_name}' has no match at or "
            f"above the ambiguity floor against {list_source} version "
            f"{list_version} (best score {round(score, 3)}). A clear is "
            "point-in-time — lists change daily — rely on the re-screen "
            "cadence."
        )

    ontology_writes: list[dict] = []
    if cp is not None:
        # Edge PROPOSAL only — the engine writes it when a human approves.
        ontology_writes.append(
            {
                "src_type": "Counterparty",
                "src_id": cp.id,
                "label": "SCREENED_ON",
                "dst_type": "SanctionsList",
                "dst_id": _LIST_ID,
                "properties": {
                    "result": result,
                    "score": round(score, 3),
                    "list_version": list_version,
                    "matched_name": matched_name,
                    "screened_at_step": 2,
                },
            }
        )
    else:
        comment += (
            " Vendor is not in the counterparty record yet — the screening "
            "result rides this decision only; no graph edge is proposed."
        )

    return WorkflowAgentOutput(
        proposed_action=proposed_action,
        target_step=target_step,
        confidence=confidence,
        comment=comment,
        citations=citations,
        ontology_writes=ontology_writes,
    )
