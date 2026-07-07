"""Data-Privacy Assessment (DPIA) agent — the ``breach_assessor`` handler of
the ``data_breach`` ladder (Privacy Ops module, PR 16).

This is the ontology-aware, cited replacement for the deterministic
``breach_assessor`` stub that shipped inside ``app/workflow/library.py``
(same move the Contracts module made for ``nda_reviewer``: the library stub
is superseded by the module implementation). Because the library still
registers its stub at import time and ``register_workflow_agent`` guards
against a double-registration, we import the library FIRST so its module
body runs and caches in ``sys.modules`` — a later ``import
app.workflow.library`` from ``main.py`` is then a no-op and cannot
re-trigger the guard — then pop the stub and register this handler in its
place. No other file is touched.

Two decision surfaces, evaluated in order:

  1. BACK-COMPAT breach-severity (unchanged contract the existing E2E tests
     key off): when ``records_affected`` is present, decide purely on the
     DPDP notification threshold (>= 1000 records → notify; else confirm).
     This branch is deterministic and never echoes the untrusted request.
  2. DPIA scoring (no ``records_affected``): a data-category taxonomy scan
     over the untrusted description / data_categories, a rating function of
     (category, volume, cross-border transfer, novel tech), and a
     governance rule that keeps HIGH-risk assessments OUT of the agent's
     hands — HIGH proposes "approve" (advance to a human) at low confidence
     with a DPO-escalation memo, so the Cockpit surfaces it prominently.

At LADDER COMPLETION the registered hook authors one ``Assessment`` row and
the processing map as ``PROCESSES`` ontology edges (Assessment →
DataCategory) — closing the one-brain loop so future privacy work sees what
we assessed and on what basis.

All free text in ``context`` is UNTRUSTED — matched with keyword scans and
quoted as data via ``spotlight``, never interpreted as instructions. The
handler only ever RECOMMENDS; its output parks as a PENDING AgentDecision
and a permissioned human approval in the Cockpit advances the ladder.
"""

from __future__ import annotations

# Import the library first so its deterministic breach_assessor stub
# registers and the module caches — see the module docstring. Then remove the
# stub so this module can claim the "breach_assessor" key without tripping the
# register-twice guard.
import app.workflow.library  # noqa: F401
from app.workflow.agents import _HANDLERS as _AGENT_HANDLERS

_AGENT_HANDLERS.pop("breach_assessor", None)

from sqlalchemy import select  # noqa: E402

from app.core.ai import AIUnavailableError, call_claude_json, spotlight  # noqa: E402
from app.core.audit import log_audit  # noqa: E402
from app.db.models import Assessment  # noqa: E402
from app.db.ontology import NodeRef, add_edge  # noqa: E402
from app.workflow.agents import (  # noqa: E402
    WorkflowAgentDeps,
    WorkflowAgentOutput,
    register_workflow_agent,
)
from app.workflow.hooks import register_completion_hook  # noqa: E402

_PLAYBOOK_ID = "DPIA-v1"
_PLAYBOOK_TITLE = "Privacy Assessment Playbook"

# ── Data-category taxonomy (keyword scan over UNTRUSTED text) ────────────
# (canonical category name, trigger substrings). First-match-per-category;
# a category is "detected" if any of its substrings appear in the scanned
# text (description + explicit data_categories).
_CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("health", ("health", "medical", "patient", "clinical", "diagnos", "mental health", "nhs")),
    ("children", ("child", "children", "minor", "kids", "under 18", "under-18", "pupil", "student")),
    ("biometric", ("biometric", "fingerprint", "facial recognition", "face recognition",
                   "retina", "iris", "voiceprint", "gait")),
    ("sensitive", ("sensitive", "racial", "ethnic", "religio", "sexual orientation",
                   "political opinion", "genetic", "trade union", "criminal record")),
    ("employee", ("employee", "staff", "workforce", "payroll", "personnel", "hr record")),
    ("personal", ("personal", "pii", "customer", "e-mail", "email", "address", "phone",
                  "user data", "data subject", "contact detail")),
]

# Categories that on their own force a HIGH rating (DPIA §sensitive).
_SENSITIVE_TRIGGER = frozenset({"health", "children", "biometric", "sensitive"})

_HIGH_VOLUME = frozenset({"high", "large", "massive", "bulk", "millions"})

# What a complete DPIA request should have covered. Absent keys become gaps
# surfaced to the reviewer (and recorded on the Assessment at completion).
_GAP_CHECKS: list[tuple[str, str]] = [
    ("retention_period", "retention period"),
    ("lawful_basis", "lawful basis"),
    ("data_subject_rights", "data-subject rights process"),
    ("sub_processors", "sub-processors"),
]


def _scan_text(context: dict) -> str:
    parts = [str(context.get("description", "") or "")]
    cats = context.get("data_categories")
    if isinstance(cats, (list, tuple)):
        parts.extend(str(c) for c in cats)
    elif cats:
        parts.append(str(cats))
    return " ".join(parts).lower()


def _detect_categories(context: dict) -> list[str]:
    text = _scan_text(context)
    detected: list[str] = []
    for name, needles in _CATEGORY_KEYWORDS:
        if any(n in text for n in needles):
            detected.append(name)
    return detected


def _rate(categories: list[str], *, volume, transfer: bool, novelty: bool) -> str:
    if any(c in _SENSITIVE_TRIGGER for c in categories) or transfer or novelty:
        return "HIGH"
    if "personal" in categories and str(volume).lower() in _HIGH_VOLUME:
        return "MEDIUM"
    return "LOW"


def _gaps(context: dict) -> list[str]:
    return [label for key, label in _GAP_CHECKS if key not in context]


def _lawful_basis(context: dict) -> str:
    return str(context.get("lawful_basis") or "consent/legitimate-interest (confirm)")


def _subject(context: dict) -> str:
    subject = context.get("subject")
    if subject:
        return str(subject)[:200]
    return str(context.get("description", "") or "")[:80] or "unspecified processing"


async def _citations(deps: WorkflowAgentDeps | None, subject: str) -> list[dict]:
    """Playbook citation always; a prior Assessment for the same subject when
    one exists (org-scoped ILIKE) — the ontology objects the agent relied on."""
    citations: list[dict] = [
        {"type": "Playbook", "id": _PLAYBOOK_ID, "title": _PLAYBOOK_TITLE}
    ]
    if deps is None or not subject:
        return citations
    prior = (
        await deps.session.execute(
            select(Assessment).where(
                Assessment.organization_id == deps.organization_id,
                Assessment.subject.ilike(f"%{subject}%"),
            )
        )
    ).scalars().first()
    if prior is not None:
        citations.append(
            {"type": "Assessment", "id": prior.id, "title": prior.subject}
        )
    return citations


@register_workflow_agent("breach_assessor")
async def breach_assessor(
    context: dict, step_config: dict, deps: WorkflowAgentDeps
) -> WorkflowAgentOutput:
    subject = _subject(context)
    gaps = _gaps(context)
    lawful_basis = _lawful_basis(context)
    citations = await _citations(deps, subject)
    gaps_line = ("Gaps: " + ", ".join(gaps)) if gaps else "Gaps: none identified"

    # ── 1. Back-compat breach-severity branch (deterministic, first) ─────
    # Present-and-int records_affected decides on the DPDP notification
    # threshold alone. Never echoes the untrusted description.
    if "records_affected" in context:
        try:
            records = int(context.get("records_affected") or 0)
        except (TypeError, ValueError):
            records = 0
        if records >= 1000:
            confidence = 0.9
            comment = (
                f"Agent: {records:,} records affected — notification threshold "
                "met; 72-hour clock running."
            )
        else:
            confidence = 0.6
            comment = (
                f"Agent: {records:,} records affected — below clear threshold; "
                "counsel should confirm notifiability."
            )
        draft = f"{comment}\n\nLawful basis (candidate): {lawful_basis}\n{gaps_line}"
        return WorkflowAgentOutput(
            proposed_action="approve",
            comment=comment,
            confidence=confidence,
            drafted_response=draft,
            citations=citations,
        )

    # ── 2. DPIA scoring branch ──────────────────────────────────────────
    categories = _detect_categories(context)
    volume = context.get("volume", "unknown")
    transfer = bool(context.get("cross_border", False))
    novelty = bool(context.get("novel_tech", False))
    rating = _rate(categories, volume=volume, transfer=transfer, novelty=novelty)

    cats_line = ", ".join(categories) if categories else "no categories detected"

    # Governance rule: HIGH never stays with the agent — it proposes
    # "approve" (advance to a human) at low confidence with a DPO escalation.
    if rating == "HIGH":
        confidence = 0.4
        comment = (
            "HIGH risk — sensitive category / cross-border / novel tech — "
            f"escalate to DPO. Data categories: {cats_line}. {gaps_line}"
        )
    elif rating == "MEDIUM":
        confidence = 0.7
        comment = (
            f"Agent: MEDIUM risk — personal data at volume. Data categories: "
            f"{cats_line}. {gaps_line}"
        )
    else:
        confidence = 0.9
        comment = (
            f"Agent: low risk — proceed with conditions. Data categories: "
            f"{cats_line}. {gaps_line}"
        )

    draft = (
        f"Privacy assessment for: {subject}\n"
        f"Risk rating: {rating}\n"
        f"Data categories: {cats_line}\n"
        f"Cross-border transfer: {'yes' if transfer else 'no'}\n"
        f"Lawful basis (candidate): {lawful_basis}\n"
        f"{gaps_line}"
    )

    # Optional Claude polish of the memo — never changes the rating/decision.
    try:
        result = await call_claude_json(
            "You are the data-privacy assessment agent for a legal operations "
            "platform. Rewrite the assessment memo below as a professional "
            "90-160 word note from a privacy officer to reviewing counsel. "
            "Keep the risk rating, data categories, gaps, and lawful-basis "
            "note exactly as given; do not add or drop findings.\n"
            + spotlight(draft, "MEMO")
            + '\nRespond ONLY as JSON: {"memo": "..."}',
            max_tokens=500,
            purpose="agent.breach_assessor",
            organization_id=deps.organization_id if deps else None,
        )
        if result.get("memo"):
            draft = result["memo"]
    except (AIUnavailableError, Exception):  # noqa: BLE001 — deterministic text stands
        pass

    return WorkflowAgentOutput(
        proposed_action="approve",
        comment=comment,
        confidence=confidence,
        drafted_response=draft,
        citations=citations,
    )


@register_completion_hook("data_breach")
async def record_privacy_assessment(session, instance) -> None:
    """When the data_breach ladder completes, author one Assessment row plus
    the processing map (Assessment → DataCategory ``PROCESSES`` edges) and a
    SYSTEM audit row. Idempotent on ``source_workflow_id`` — replays never
    duplicate the assessment.
    """
    existing = (
        await session.execute(
            select(Assessment).where(
                Assessment.organization_id == instance.organization_id,
                Assessment.source_workflow_id == instance.id,
            )
        )
    ).scalars().first()
    if existing is not None:
        return  # idempotent replay

    context = instance.context or {}
    categories = _detect_categories(context)
    volume = context.get("volume", "unknown")
    transfer = bool(context.get("cross_border", False))
    novelty = bool(context.get("novel_tech", False))
    rating = _rate(categories, volume=volume, transfer=transfer, novelty=novelty)
    lawful_basis = _lawful_basis(context)
    subject = _subject(context)
    gaps = _gaps(context)

    raw_conditions = context.get("conditions")
    conditions = [str(c) for c in raw_conditions] if isinstance(raw_conditions, (list, tuple)) else []

    assessment = Assessment(
        organization_id=instance.organization_id,
        subject=subject,
        risk_rating=rating,
        lawful_basis=lawful_basis,
        data_categories=categories,
        cross_border=transfer,
        conditions=conditions,
        gaps=gaps,
        source_workflow_id=instance.id,
    )
    session.add(assessment)
    await session.flush()

    for category in categories:
        await add_edge(
            session,
            organization_id=instance.organization_id,
            src=NodeRef("Assessment", assessment.id),
            label="PROCESSES",
            dst=NodeRef("DataCategory", category),
            properties={"workflow_instance_id": instance.id},
            source_module="privacy",
            created_by=instance.started_by,
        )

    await log_audit(
        session,
        organization_id=instance.organization_id,
        actor_id=None,
        actor_type="SYSTEM",
        action="assessment.created",
        resource_type="Assessment",
        resource_id=assessment.id,
        after_json={
            "subject": subject,
            "risk_rating": rating,
            "lawful_basis": lawful_basis,
            "data_categories": categories,
            "cross_border": transfer,
        },
        metadata={
            "source": "data_breach completion",
            "workflow_instance_id": instance.id,
        },
    )
