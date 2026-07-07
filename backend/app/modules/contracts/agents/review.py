"""Contract Review Agent v2 — deterministic risk core + document checklist,
ontology-cited, human-gated (the `contract_risk_reviewer` step of the
clm_contract_approval ladder).

Layered design:

  1. DETERMINISTIC CORE (preserved from the v1 stub — E2E tests key off
     these context fields): missing liability-cap / termination clauses
     send the draft back; risk_score >= 8 sends back; risk_score >= 5 or
     contract_value >= $1M approves at low confidence ("elevated risk");
     otherwise approves high-confidence.
  2. RISK-TERM CHECKLIST over the uploaded document's extracted text —
     regex-only over UNTRUSTED input (uncapped liability, auto-renewal,
     broad IP assignment, unilateral termination, governing-law absence).
     Reject-level findings (uncapped liability, IP assignment) cap
     confidence at 0.55 and require senior counsel review.
  3. ONTOLOGY READS: the counterparty resolves through the same shared
     graph humans use; prior PARTY_TO contract documents are counted and
     cited — no private agent data path.
  4. Optional Claude polish of the memo (never the decision). On any AI
     failure the deterministic text stands (degrade discipline).

The handler only ever RECOMMENDS — its output parks as a PENDING
AgentDecision and the Cockpit approval advances the ladder.

At LADDER COMPLETION the registered hook writes the contract's Obligation
rows (from context, plus an auto-renewal obligation when the deterministic
scan found one) and OBLIGATES edges — closing the one-brain loop so
downstream obligation tracking sees what the contract committed us to.

All free text in ``context`` and ``Document.extracted_text`` is UNTRUSTED —
matched with regex, quoted into memos as data via ``spotlight``, never
interpreted as instructions.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.ai import AIUnavailableError, call_claude_json, spotlight
from app.core.audit import log_audit
from app.db.models import Counterparty, Document, Obligation
from app.db.ontology import NodeRef, add_edge, get_neighbors
from app.workflow.agents import (
    WorkflowAgentDeps,
    WorkflowAgentOutput,
    register_workflow_agent,
)
from app.workflow.hooks import register_completion_hook

_CHECKLIST_ID = "CONTRACT-RISK-CHECKLIST-v1"

# Same extraction approach as the NDA agent: regex over the untrusted
# description; an explicit context["counterparty"] wins.
_COUNTERPARTY_RE = re.compile(
    r"(?:with|for)\s+([A-Z][A-Za-z0-9&. ]{2,50}?)(?:\s+(?:re\.|regarding|for|by|about|,|\.|\n)|$)"
)

# ── Deterministic risk-term checklist (regex-only over untrusted text) ──
# (key, pattern, severity, issue, suggested position, reject_level)
_AUTO_RENEWAL_RE = re.compile(r"automatically\s+renew", re.IGNORECASE)

_CHECKLIST: list[tuple[str, re.Pattern, str, str, str, bool]] = [
    (
        "uncapped_liability",
        re.compile(r"unlimited liability|no limitation of liability", re.IGNORECASE),
        "HIGH",
        "Uncapped liability exposure",
        "Cap liability at 12 months' fees and exclude consequential damages",
        True,
    ),
    (
        "ip_assignment",
        re.compile(r"assigns? all right,? title", re.IGNORECASE),
        "HIGH",
        "Broad IP assignment (all right, title and interest)",
        "Limit assignment to project deliverables; retain background IP",
        True,
    ),
    (
        "auto_renewal",
        _AUTO_RENEWAL_RE,
        "MEDIUM",
        "Auto-renewal clause",
        "Require a 60-day non-renewal notice window and calendar the renewal date",
        False,
    ),
    (
        "unilateral_termination",
        re.compile(r"terminate at any time without", re.IGNORECASE),
        "MEDIUM",
        "Unilateral termination without cause or notice",
        "Require 30-day written notice with mutual termination rights",
        False,
    ),
]

_GOVERNING_LAW_RE = re.compile(r"governing law|governed by", re.IGNORECASE)


def _extract_counterparty(context: dict) -> str | None:
    explicit = context.get("counterparty")
    if explicit:
        return str(explicit)[:120]
    match = _COUNTERPARTY_RE.search(context.get("description", "") or "")
    return match.group(1).strip() if match else None


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _scan_text(text: str) -> tuple[list[str], bool]:
    """Run the deterministic checklist over UNTRUSTED extracted text.

    Returns (issue lines "SEVERITY — issue — suggested position",
    any_reject_level_finding).
    """
    issues: list[str] = []
    reject = False
    for _key, pattern, severity, issue, position, reject_level in _CHECKLIST:
        if pattern.search(text):
            issues.append(f"{severity} — {issue} — {position}")
            reject = reject or reject_level
    if not _GOVERNING_LAW_RE.search(text):
        issues.append(
            "MEDIUM — No governing-law clause found — "
            "Add an express governing-law and jurisdiction clause"
        )
    return issues, reject


@register_workflow_agent("contract_risk_reviewer")
async def contract_risk_reviewer(
    context: dict, step_config: dict, deps: WorkflowAgentDeps
) -> WorkflowAgentOutput:
    risk_score = _num(context.get("risk_score"))
    contract_value = _num(context.get("contract_value"))

    missing: list[str] = []
    if not context.get("has_liability_cap", True):
        missing.append("liability cap")
    if not context.get("has_termination_clause", True):
        missing.append("termination clause")

    # ── 1. Deterministic core (v1 stub semantics, preserved) ────────────
    target_step: int | None = None
    if missing:
        action, target_step, confidence = "send_back", 1, 0.95
        comment = (
            f"Agent: required clauses missing — {', '.join(missing)}. "
            "Returning to the drafter to add them before legal review."
        )
    elif risk_score >= 8:
        action, target_step, confidence = "send_back", 1, 0.9
        comment = (
            f"Agent: risk score {risk_score:g}/10 exceeds the critical "
            "threshold — returning to the drafter for de-risking."
        )
    elif risk_score >= 5 or contract_value >= 1_000_000:
        action, confidence = "approve", 0.55
        comment = "Agent: elevated risk — please review findings."
    else:
        action, confidence = "approve", 0.93
        comment = "Agent: deterministic checks passed — standard commercial terms."

    citations: list[dict] = [
        {"type": "Playbook", "id": _CHECKLIST_ID, "title": "Contract Risk Checklist"}
    ]

    # ── 2. Ontology reads: counterparty + prior contract documents ──────
    counterparty_name = _extract_counterparty(context)
    if counterparty_name:
        cp = (
            await deps.session.execute(
                select(Counterparty).where(
                    Counterparty.organization_id == deps.organization_id,
                    Counterparty.name.ilike(f"%{counterparty_name}%"),
                )
            )
        ).scalars().first()
        if cp is not None:
            citations.append({"type": "Counterparty", "id": cp.id, "title": cp.name})
            edges = await get_neighbors(
                deps.session,
                organization_id=deps.organization_id,
                node=NodeRef("Counterparty", cp.id),
                labels=["PARTY_TO"],
                direction="both",
            )
            prior_docs = {
                (e.src_type, e.src_id) if e.src_type == "Document" else (e.dst_type, e.dst_id)
                for e in edges
                if "Document" in (e.src_type, e.dst_type)
            }
            if prior_docs:
                comment += (
                    f" {len(prior_docs)} prior contract document"
                    f"{'s' if len(prior_docs) != 1 else ''} with this "
                    "counterparty on file."
                )

    # ── 3. Risk-term checklist over the uploaded document ───────────────
    issues: list[str] = []
    document_id = context.get("document_id")
    if document_id:
        doc = (
            await deps.session.execute(
                select(Document).where(
                    Document.id == str(document_id),
                    Document.organization_id == deps.organization_id,  # org check
                )
            )
        ).scalars().first()
        if doc is None:
            comment += (
                " GAP — referenced document not found in this organization — "
                "review the source document manually."
            )
            confidence = min(confidence, 0.6)
        elif not (doc.extracted_text or "").strip():
            comment += (
                " GAP — binary upload not yet parsed — review the source "
                "document manually."
            )
            confidence = min(confidence, 0.6)
        else:
            citations.append({"type": "Document", "id": doc.id, "title": doc.name})
            issues, reject_level = _scan_text(doc.extracted_text)
            if issues:
                comment += " Findings: " + " | ".join(issues)
            if reject_level:
                confidence = min(confidence, 0.55)
                comment += (
                    " Reject-level risk terms present — senior counsel "
                    "review required before approval."
                )

    # ── Memo (drafted_response) — issue-list memo the approver may edit ─
    memo_parts = [comment]
    if issues:
        memo_parts.append(
            "Checklist findings:\n" + "\n".join(f"- {line}" for line in issues)
        )
    draft = "\n\n".join(memo_parts)

    # ── 4. Optional Claude polish — NEVER changes action/confidence ─────
    try:
        result = await call_claude_json(
            "You are the contract risk-review agent for a legal operations "
            "platform. Rewrite the memo below as a professional 90-160 word "
            "note from a senior contracts paralegal to the reviewing lawyer. "
            "Keep every factual claim, severity, and finding exactly as "
            "given; do not add or remove findings or commitments.\n"
            + spotlight(draft, "MEMO")
            + '\nRespond ONLY as JSON: {"memo": "..."}',
            max_tokens=500,
            purpose="agent.contract_risk_reviewer",
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


def _parse_due_date(raw) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@register_completion_hook("clm_contract_approval")
async def record_contract_obligations(session, instance) -> None:
    """When the contract approval ladder completes, persist the contract's
    obligations: rows from ``context["obligations"]`` plus an auto-renewal
    obligation when the deterministic scan found an auto-renewal clause in
    the uploaded document. Each obligation gets an OBLIGATES edge from the
    intake ticket and a SYSTEM audit row.

    Idempotent: keyed on (organization_id, source_type, source_id,
    description) — replays/retries never duplicate an obligation.
    """
    context = instance.context or {}

    specs: list[dict] = []
    for ob in context.get("obligations") or []:
        if isinstance(ob, dict) and ob.get("description"):
            specs.append(
                {
                    "description": str(ob["description"])[:2000],
                    "due_date": _parse_due_date(ob.get("due_date")),
                }
            )

    # Deterministic auto-renewal finding → a tracked obligation.
    document_id = context.get("document_id")
    if document_id:
        doc = (
            await session.execute(
                select(Document).where(
                    Document.id == str(document_id),
                    Document.organization_id == instance.organization_id,
                )
            )
        ).scalars().first()
        if doc is not None and _AUTO_RENEWAL_RE.search(doc.extracted_text or ""):
            specs.append(
                {
                    "description": (
                        "Auto-renewal: this contract renews automatically — "
                        "calendar the non-renewal notice deadline."
                    ),
                    "due_date": None,
                }
            )

    for spec in specs:
        existing = (
            await session.execute(
                select(Obligation).where(
                    Obligation.organization_id == instance.organization_id,
                    Obligation.source_type == "CONTRACT",
                    Obligation.source_id == instance.entity_id,
                    Obligation.description == spec["description"],
                )
            )
        ).scalars().first()
        if existing is not None:
            continue  # idempotent replay

        obligation = Obligation(
            organization_id=instance.organization_id,
            source_type="CONTRACT",
            source_id=instance.entity_id,
            description=spec["description"],
            due_date=spec["due_date"],
            status="OPEN",
        )
        session.add(obligation)
        await session.flush()

        await add_edge(
            session,
            organization_id=instance.organization_id,
            src=NodeRef("IntakeTicket", instance.entity_id),
            label="OBLIGATES",
            dst=NodeRef("Obligation", obligation.id),
            properties={"workflow_instance_id": instance.id},
            source_module="contracts",
            created_by=instance.started_by,
        )
        await log_audit(
            session,
            organization_id=instance.organization_id,
            actor_id=None,
            actor_type="SYSTEM",
            action="obligation.created",
            resource_type="Obligation",
            resource_id=obligation.id,
            after_json={
                "description": obligation.description,
                "source_type": "CONTRACT",
                "source_id": instance.entity_id,
                "status": "OPEN",
            },
            metadata={
                "source": "clm_contract_approval completion",
                "workflow_instance_id": instance.id,
            },
        )
