"""Contract-Type Specialist Agent (PR 19) — the ``contract_type_specialist``
step of the like-named ladder.

The generalist ``contract_risk_reviewer`` (review.py) runs one fixed risk
checklist over every commercial contract. The specialist keeps that
checklist mindset but adds the differentiator: **per-type playbook bands**.
One configurable agent, many counsel-owned playbooks — no new codebase per
contract type.

Design:

  1. PLAYBOOK SELECTION (the differentiator). ``context["contract_type"]``
     (e.g. "vendor" / "services" / "clinical") selects the active,
     latest-version ``ContractPlaybook`` for the org. If none matches the
     agent FALLS THROUGH — a low-confidence generalist-review approval whose
     comment names the miss so ops can track fallthrough volume and promote
     high-frequency types into a real playbook. When a playbook IS found the
     comment + citation name it and its version (the approver's first check:
     which standard, which version).

  2. BENCHMARKING against the selected playbook — deterministic over the
     JSONB ``context`` plus the optional uploaded ``Document.extracted_text``
     (regex only; anything sent to Claude is spotlighted):
       * a MANDATORY clause missing  -> send_back to the drafter;
       * a FORBIDDEN clause present  -> send_back to the drafter;
       * a NEGOTIABLE value out of band -> approve, but at low confidence
         (a lawyer eyeballs the negotiated term);
       * all clear -> approve within the playbook's bands.

  3. Optional Claude memo polish (never changes the action or confidence —
     degrade discipline: on any AI failure the deterministic text stands).

The handler only ever RECOMMENDS — its output parks as a PENDING
AgentDecision and the Cockpit approval advances the ladder (conservative-AI
rule #1).

At LADDER COMPLETION the registered hook writes a PLAYBOOK_APPLIED ontology
edge from the intake ticket to the ContractPlaybook it was benchmarked
against, carrying the version — reproducibility: which standard, which
version, applied to this contract.

All free text in ``context`` and ``Document.extracted_text`` is UNTRUSTED —
matched with regex, quoted into memos as data via ``spotlight``, never
interpreted as instructions.
"""

from __future__ import annotations

from sqlalchemy import select

from app.core.ai import AIUnavailableError, call_claude_json, spotlight
from app.core.audit import log_audit
from app.db.models import ContractPlaybook, Document
from app.db.ontology import NodeRef, add_edge, get_neighbors
from app.workflow.agents import (
    WorkflowAgentDeps,
    WorkflowAgentOutput,
    register_workflow_agent,
)
from app.workflow.hooks import register_completion_hook


def _num(value, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt(value: float) -> str:
    return f"{value:g}"


def _keyword(clause: str) -> str:
    """A clause key like ``liability_cap`` matches the phrase ``liability cap``
    in extracted text (best-effort, case-insensitive)."""
    return str(clause).replace("_", " ").lower()


async def _select_playbook(
    deps: WorkflowAgentDeps, contract_type: str | None
) -> ContractPlaybook | None:
    """Active, latest-version playbook for (org, contract_type)."""
    if not contract_type:
        return None
    return (
        await deps.session.execute(
            select(ContractPlaybook)
            .where(
                ContractPlaybook.organization_id == deps.organization_id,
                ContractPlaybook.contract_type == contract_type,
                ContractPlaybook.is_active.is_(True),
            )
            .order_by(ContractPlaybook.version.desc())
        )
    ).scalars().first()


async def _load_extracted_text(deps: WorkflowAgentDeps, context: dict) -> str:
    """Fetch the uploaded document's extracted text, if any. UNTRUSTED."""
    document_id = context.get("document_id")
    if not document_id:
        return ""
    doc = (
        await deps.session.execute(
            select(Document).where(
                Document.id == str(document_id),
                Document.organization_id == deps.organization_id,  # org check
            )
        )
    ).scalars().first()
    return (doc.extracted_text or "") if doc is not None else ""


@register_workflow_agent("contract_type_specialist")
async def contract_type_specialist(
    context: dict, step_config: dict, deps: WorkflowAgentDeps
) -> WorkflowAgentOutput:
    contract_type = context.get("contract_type")
    playbook = await _select_playbook(deps, contract_type)

    # ── Fallthrough: no playbook for this type ─────────────────────────
    if playbook is None:
        comment = (
            f"Agent: no playbook for type '{contract_type}' — generalist "
            "review applies (track fallthrough volume so high-frequency types "
            "get promoted). fallthrough."
        )
        return WorkflowAgentOutput(
            proposed_action="approve",
            confidence=0.5,
            comment=comment,
            drafted_response=comment,
            citations=[],
        )

    version = playbook.version
    text = (await _load_extracted_text(deps, context)).lower()

    # ── Benchmarking against the selected playbook (deterministic) ─────
    issues: list[str] = []
    mandatory_missing: list[str] = []
    forbidden_present: list[str] = []
    out_of_band: list[str] = []

    for clause in playbook.mandatory_clauses or []:
        flag_present = bool(context.get(f"has_{clause}", True))
        text_present = True
        if text:
            text_present = _keyword(clause) in text
        if not flag_present or not text_present:
            mandatory_missing.append(str(clause))
            issues.append(f"MANDATORY MISSING: {clause}")

    for clause in playbook.forbidden_clauses or []:
        flagged = bool(context.get(f"has_{clause}", False))
        in_text = bool(text) and _keyword(clause) in text
        if flagged or in_text:
            forbidden_present.append(str(clause))
            issues.append(f"FORBIDDEN PRESENT: {clause}")

    for key, band in (playbook.negotiable_bands or {}).items():
        if key not in context:
            continue
        value = _num(context.get(key))
        if value is None or not isinstance(band, (list, tuple)) or len(band) != 2:
            continue
        lo, hi = band
        if value < lo or value > hi:
            out_of_band.append(f"{key}={_fmt(value)}")
            issues.append(f"OUT OF BAND: {key}={_fmt(value)} (band {lo}-{hi})")

    # ── Decision ───────────────────────────────────────────────────────
    target_step: int | None = None
    if forbidden_present:
        action, target_step, confidence = "send_back", 1, 0.95
        headline = "FORBIDDEN PRESENT: " + ", ".join(forbidden_present)
        if mandatory_missing:
            headline += " | MANDATORY MISSING: " + ", ".join(mandatory_missing)
        headline += " — returning to the drafter."
    elif mandatory_missing:
        action, target_step, confidence = "send_back", 1, 0.9
        headline = (
            "MANDATORY MISSING: " + ", ".join(mandatory_missing)
            + " — returning to the drafter to add them before legal review."
        )
    elif out_of_band:
        action, confidence = "approve", 0.6
        headline = (
            "OUT OF BAND: " + ", ".join(out_of_band)
            + " — within mandatory/forbidden requirements; approving at reduced "
            "confidence for a lawyer to review the negotiated term."
        )
    else:
        action, confidence = "approve", 0.9
        headline = (
            f"within {contract_type} playbook v{version} bands (all mandatory "
            "clauses present, no forbidden clauses, negotiated terms in band)"
        )

    comment = f"Agent: playbook={contract_type} v{version} — {headline}"

    citations: list[dict] = [
        {
            "type": "ContractPlaybook",
            "id": playbook.id,
            "title": f"{contract_type} playbook v{version}",
        }
    ]

    # ── Memo (drafted_response) — issue list + benchmark summary ───────
    memo_parts = [comment]
    if issues:
        memo_parts.append("Findings:\n" + "\n".join(f"- {line}" for line in issues))
    memo_parts.append(
        f"Benchmarked against the {contract_type} playbook v{version}: "
        f"mandatory [{', '.join(str(c) for c in (playbook.mandatory_clauses or []))}]; "
        f"forbidden [{', '.join(str(c) for c in (playbook.forbidden_clauses or []))}]; "
        f"negotiable bands {dict(playbook.negotiable_bands or {})}."
    )
    draft = "\n\n".join(memo_parts)

    # ── Optional Claude polish — NEVER changes action/confidence ───────
    try:
        result = await call_claude_json(
            "You are the contract-type-specialist agent for a legal operations "
            "platform. Rewrite the memo below as a professional 90-160 word "
            "note from a senior contracts paralegal to the reviewing lawyer. "
            "Keep every factual claim, finding, clause name, and the named "
            "playbook and version exactly as given; do not add or remove "
            "findings.\n"
            + spotlight(draft, "MEMO")
            + '\nRespond ONLY as JSON: {"memo": "..."}',
            max_tokens=500,
            purpose="agent.contract_type_specialist",
            organization_id=deps.organization_id,
        )
        if result.get("memo"):
            draft = result["memo"]
    except (AIUnavailableError, Exception):  # noqa: BLE001 — deterministic text stands
        pass

    return WorkflowAgentOutput(
        proposed_action=action,
        target_step=target_step,
        comment=comment,
        confidence=confidence,
        drafted_response=draft,
        citations=citations,
    )


@register_completion_hook("contract_type_specialist")
async def record_playbook_applied(session, instance) -> None:
    """When the specialist ladder completes, write a PLAYBOOK_APPLIED edge from
    the intake ticket to the ContractPlaybook it was benchmarked against —
    reproducibility: which standard, which version, applied to this contract.

    Only fires when a playbook was actually selected (the fallthrough path
    applied no standard, so it records none — the agent's comment/log noted
    the miss). Idempotent on the edge identity; the SYSTEM audit row is
    written only when the edge is newly created.
    """
    context = instance.context or {}
    contract_type = context.get("contract_type")
    if not contract_type:
        return

    playbook = (
        await session.execute(
            select(ContractPlaybook)
            .where(
                ContractPlaybook.organization_id == instance.organization_id,
                ContractPlaybook.contract_type == contract_type,
                ContractPlaybook.is_active.is_(True),
            )
            .order_by(ContractPlaybook.version.desc())
        )
    ).scalars().first()
    if playbook is None:
        return  # fallthrough — no standard applied, no edge

    # Idempotency: skip both the edge refresh and the audit row on replay.
    existing = await get_neighbors(
        session,
        organization_id=instance.organization_id,
        node=NodeRef("IntakeTicket", instance.entity_id),
        labels=["PLAYBOOK_APPLIED"],
        direction="out",
    )
    if any(
        e.dst_type == "ContractPlaybook" and e.dst_id == playbook.id
        for e in existing
    ):
        return

    await add_edge(
        session,
        organization_id=instance.organization_id,
        src=NodeRef("IntakeTicket", instance.entity_id),
        label="PLAYBOOK_APPLIED",
        dst=NodeRef("ContractPlaybook", playbook.id),
        properties={
            "version": playbook.version,
            "contract_type": contract_type,
            "workflow_instance_id": instance.id,
        },
        source_module="contracts",
        created_by=instance.started_by,
    )
    await log_audit(
        session,
        organization_id=instance.organization_id,
        actor_id=None,
        actor_type="SYSTEM",
        action="contract.playbook_applied",
        resource_type="ContractPlaybook",
        resource_id=playbook.id,
        after_json={
            "contract_type": contract_type,
            "version": playbook.version,
            "entity_type": instance.entity_type,
            "entity_id": instance.entity_id,
        },
        metadata={
            "source": "contract_type_specialist completion",
            "workflow_instance_id": instance.id,
        },
    )
