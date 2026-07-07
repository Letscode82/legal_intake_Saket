"""Notice Management Agent (PR 15) — the ``notice_analyzer`` step of the
``legal_notice`` ladder.

A legal / statutory / regulatory notice arrives with a hard reply clock. The
agent's job is narrow and deterministic:

  1. TAXONOMY → URGENCY. Classify the notice (regulatory / statutory /
     breach-termination / demand / informational) from the UNTRUSTED
     description using regex only, and rank urgency
     regulatory > statutory > breach > demand > informational.
  2. DEADLINE EXTRACTION. Prefer an explicit ``reply_deadline_days`` in the
     ladder context; otherwise regex the description for a reply window.
     Regulatory / statutory notices, or any window under seven days, are
     HIGH urgency.
  3. SENDER / COUNTERPARTY. If a sender is named (or resolves against the
     shared Counterparty graph via ILIKE) cite it and note, generically,
     that prior contract provisions may bear on the response.
  4. A minimal, safe acknowledgment draft plus a one-paragraph situation
     brief quoting the SOURCE phrase the deadline came from.

The handler only ever RECOMMENDS ``approve`` — it advances the ladder to
human drafting; the agent NEVER sends anything. All free text in ``context``
is UNTRUSTED: it is matched with regex and never interpreted as
instructions. The true control is the downstream human-approval gate, which
no prompt content can remove.

At LADDER COMPLETION the registered hook writes one tracked ``Obligation``
per extracted deadline (with an OBLIGATES edge from the intake ticket and a
``notice.sla_started`` ``Event``) — closing the one-brain loop so downstream
obligation tracking sees the reply clock the notice started.
"""

from __future__ import annotations

import re

from sqlalchemy import select

from app.core.audit import log_audit
from app.db.models import Counterparty, Event, Obligation
from app.db.ontology import NodeRef, add_edge
from app.workflow.agents import (
    WorkflowAgentDeps,
    WorkflowAgentOutput,
    register_workflow_agent,
)
from app.workflow.hooks import register_completion_hook

_PLAYBOOK = {"type": "Playbook", "id": "NOTICE-TRIAGE-v1", "title": "Notice Triage Playbook"}

# ── Notice taxonomy (regex-only over UNTRUSTED description) ──────────────
# Ordered highest-urgency first; first family that matches wins.
_TAXONOMY: list[tuple[str, re.Pattern]] = [
    ("regulatory", re.compile(r"usfda|regulator|sebi|authority", re.IGNORECASE)),
    ("statutory", re.compile(r"statutory|summons|court", re.IGNORECASE)),
    ("breach", re.compile(r"breach|terminate|default", re.IGNORECASE)),
    ("demand", re.compile(r"demand|pay within", re.IGNORECASE)),
]

# Base urgency band per category; a sub-seven-day window escalates to HIGH.
_BASE_URGENCY = {
    "regulatory": "HIGH",
    "statutory": "HIGH",
    "breach": "MEDIUM",
    "demand": "MEDIUM",
    "informational": "LOW",
}

_WITHIN_DAYS_RE = re.compile(r"within\s+(\d+)\s+days", re.IGNORECASE)
_N_DAY_RE = re.compile(r"(\d+)-day", re.IGNORECASE)


def _classify_notice(description: str) -> str:
    """Deterministic taxonomy; falls through to ``informational``."""
    text = description or ""
    for category, pattern in _TAXONOMY:
        if pattern.search(text):
            return category
    return "informational"


def _extract_deadline(context: dict) -> tuple[int | None, str | None]:
    """Return ``(days, source_phrase)``. Prefer the explicit context field;
    otherwise regex the UNTRUSTED description. ``source_phrase`` quotes the
    text the number came from — never the whole description."""
    explicit = context.get("reply_deadline_days")
    if explicit is not None:
        try:
            return int(explicit), f"reply_deadline_days = {int(explicit)}"
        except (TypeError, ValueError):
            pass
    description = context.get("description", "") or ""
    match = _WITHIN_DAYS_RE.search(description) or _N_DAY_RE.search(description)
    if match:
        return int(match.group(1)), match.group(0)
    return None, None


def _resolve_urgency(category: str, deadline: int | None) -> str:
    if category in ("regulatory", "statutory") or (deadline is not None and deadline < 7):
        return "HIGH"
    return _BASE_URGENCY.get(category, "LOW")


@register_workflow_agent("notice_analyzer")
async def notice_analyzer(
    context: dict, step_config: dict, deps: WorkflowAgentDeps
) -> WorkflowAgentOutput:
    description = context.get("description", "") or ""
    category = _classify_notice(description)
    deadline, source_phrase = _extract_deadline(context)
    urgency = _resolve_urgency(category, deadline)

    citations: list[dict] = [dict(_PLAYBOOK)]

    # ── Decision: the agent RECOMMENDS approve (advance to human drafting);
    #    it never sends. HIGH urgency is high-confidence; a missing deadline
    #    is low-confidence and asks the drafter to verify. ────────────────
    high = category in ("regulatory", "statutory") or (deadline is not None and deadline < 7)
    if high:
        confidence = 0.85
        comment = (
            f"Agent: {category} notice — {urgency} urgency. "
            + (
                f"Reply window of {deadline} day(s) extracted."
                if deadline is not None
                else "Reply window not stated — verify against the notice."
            )
            + " Advancing to human drafting; the agent does not send a response."
        )
    elif deadline is None:
        confidence = 0.55
        comment = (
            f"Agent: {category} notice — {urgency} urgency. Could not extract a "
            "reply deadline — verify against the notice before drafting."
        )
    else:
        confidence = 0.75
        comment = (
            f"Agent: {category} notice — {urgency} urgency. Reply window of "
            f"{deadline} day(s) extracted. Advancing to human drafting."
        )

    # ── Sender / Counterparty (shared graph, org-scoped ILIKE) ──────────
    sender = context.get("sender")
    if sender and deps is not None:
        cp = (
            await deps.session.execute(
                select(Counterparty).where(
                    Counterparty.organization_id == deps.organization_id,
                    Counterparty.name.ilike(f"%{str(sender)[:120]}%"),
                )
            )
        ).scalars().first()
        if cp is not None:
            citations.append({"type": "Counterparty", "id": cp.id, "title": cp.name})
            comment += (
                f" Sender {cp.name} is on file — prior contract provisions with "
                "this counterparty may bear on the response; review before drafting."
            )
        else:
            comment += (
                " Sender named but not on file — prior contract provisions, if "
                "any, should be reviewed before drafting."
            )

    # ── Minimal, safe acknowledgment draft (deliberately minimal) ───────
    ack_draft = (
        "Dear Sir/Madam,\n\n"
        "We acknowledge receipt of your notice. The matter has been referred to "
        "our legal team, which will review it and respond within the applicable "
        "period. We expressly reserve all of our rights, remedies and defences; "
        "nothing in this acknowledgment is an acceptance of the matters asserted.\n\n"
        "Regards,\nLegal Department"
    )
    if deadline is not None:
        brief = (
            f"Situation brief: {category} notice at {urgency} urgency; an extracted "
            f"reply window of {deadline} day(s), taken from the source text "
            f"{source_phrase!r}. The window is NOT resolved to a calendar date — "
            "verify it against the notice."
        )
    else:
        brief = (
            f"Situation brief: {category} notice at {urgency} urgency; no reply "
            "deadline could be extracted — verify against the notice."
        )
    drafted_response = (
        "ACKNOWLEDGMENT DRAFT (minimal by design; do not broaden without counsel):\n"
        f"{ack_draft}\n\n{brief}"
    )

    return WorkflowAgentOutput(
        proposed_action="approve",
        target_step=None,
        comment=comment,
        confidence=confidence,
        drafted_response=drafted_response,
        citations=citations,
    )


@register_completion_hook("legal_notice")
async def record_notice_obligations(session, instance) -> None:
    """When the legal-notice ladder completes, persist the reply clock the
    notice started: one ``Obligation`` per extracted deadline, an OBLIGATES
    edge from the intake ticket, and a ``notice.sla_started`` ``Event``.

    Deadlines are re-derived from ``instance.context`` (not from the agent
    decision) so the hook is self-contained and idempotent. Idempotency:
    the obligation is keyed on (organization_id, source_type, source_id,
    description); the event on (organization_id, type, source_id).
    """
    context = instance.context or {}
    description = context.get("description", "") or ""
    category = _classify_notice(description)
    deadline, source_phrase = _extract_deadline(context)
    urgency = _resolve_urgency(category, deadline)

    # ── Obligation per extracted deadline ───────────────────────────────
    if deadline is not None:
        source_type = "REGULATION" if category == "regulatory" else "CONTRACT"
        ob_description = (
            f"Respond to notice within {deadline} day(s) of receipt "
            f"(reply window from {source_phrase!r}; not resolved to a calendar "
            "date — verify against the notice)."
        )[:2000]

        existing = (
            await session.execute(
                select(Obligation).where(
                    Obligation.organization_id == instance.organization_id,
                    Obligation.source_type == source_type,
                    Obligation.source_id == instance.entity_id,
                    Obligation.description == ob_description,
                )
            )
        ).scalars().first()

        if existing is None:
            obligation = Obligation(
                organization_id=instance.organization_id,
                source_type=source_type,
                source_id=instance.entity_id,
                description=ob_description,
                # Cannot compute a real calendar date from a day-count alone —
                # leave None; the day-count lives in the description.
                due_date=None,
                status="OPEN",
                payload_metadata={"deadline_days": deadline, "category": category},
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
                source_module="regulatory",
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
                    "source_type": source_type,
                    "source_id": instance.entity_id,
                    "status": "OPEN",
                },
                metadata={
                    "source": "legal_notice completion",
                    "workflow_instance_id": instance.id,
                    "deadline_days": deadline,
                    "urgency": urgency,
                },
            )

    # ── notice.sla_started event (idempotent on org + type + source) ────
    existing_event = (
        await session.execute(
            select(Event).where(
                Event.organization_id == instance.organization_id,
                Event.type == "notice.sla_started",
                Event.source_id == instance.entity_id,
            )
        )
    ).scalars().first()
    if existing_event is None:
        session.add(
            Event(
                organization_id=instance.organization_id,
                type="notice.sla_started",
                source_type="IntakeTicket",
                source_id=instance.entity_id,
                actor_id=None,
                summary=urgency,
                payload={"deadline_days": deadline, "category": category},
            )
        )
        await session.flush()
