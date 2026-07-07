"""Trademark Clearance agent — preliminary distinctiveness + portfolio scan.

Step 2 of the ``trademark_clearance`` ladder (agent key
``trademark_clearance_reviewer``). The step's ``min_confidence`` is 0.99 —
DELIBERATELY unreachable: every recommendation this agent produces surfaces
the below-threshold concern in the Cockpit, because counsel sign-off on a
trademark is mandatory, no exceptions. The agent only ever RECOMMENDS
"approve" (advancing to the IP-lead step IS the escalation path); conflicts
and weaknesses travel in the memo, never as a send_back.

Distinctiveness spectrum (Abercrombie), applied with a deterministic
heuristic — auditable, same input → same classification:

  generic       the mark IS the goods (mark token appears verbatim in the
                goods/services text) ......................... confidence 0.20
  descriptive   the mark is built from common descriptive English words or
                from the goods tokens themselves (e.g. "QuickLegal" for
                legal services) .............................. confidence 0.40
  suggestive    the mark hints at the goods (shares a 4+ char stem with a
                goods token without containing the word) ..... confidence 0.60
  arbitrary     a real dictionary word unrelated to the goods
                ("Apple" for software) ....................... confidence 0.80
  fanciful      a coined token in no wordlist ................ confidence 0.80

All confidences sit BELOW the 0.99 threshold by design. A portfolio-conflict
hit (difflib similarity against the org's ``Mark`` rows: ratio >= 0.75, or a
shared Nice class AND ratio >= 0.6) caps confidence at 0.35.

This is portfolio context, NOT a registry search — the memo carries the
mandatory banner saying exactly that, verbatim, every time.

Ontology writes happen at LADDER COMPLETION via the registered hook: the
cleared-for-filing mark becomes a PENDING ``Mark`` row plus a clearance-memo
``Document`` node linked by a CONCERNS edge. Documented gap: spawning a
Matter on approval is deferred until the Matter module ships CRUD.

All free text in ``context`` (description, goods/services, the mark itself)
is UNTRUSTED input — matched with regex and quoted into memos as data,
never interpreted as instructions.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from sqlalchemy import func, select

from app.core.audit import log_audit
from app.db.models import Document, Mark
from app.db.ontology import NodeRef, add_edge
from app.workflow.agents import (
    WorkflowAgentDeps,
    WorkflowAgentOutput,
    register_workflow_agent,
)
from app.workflow.hooks import register_completion_hook

_PLAYBOOK_ID = "TM-CLEARANCE-v1"

_MANDATORY_BANNER = (
    "PRELIMINARY ASSESSMENT — NOT A REGISTRY SEARCH. Identical or confusingly "
    "similar registered marks may exist that this analysis cannot see. A formal "
    "registry search (USPTO/EUIPO/WIPO) and counsel sign-off are required "
    "before use."
)

# Modest built-in wordlist of generic / descriptive commercial English —
# a mark assembled from these (or from the goods text itself) cannot carry
# strong inherent distinctiveness.
_DESCRIPTIVE_WORDS = frozenset({
    "legal", "software", "cloud", "data", "shop", "store", "best", "quick",
    "fresh", "smart", "easy", "fast", "tech", "digital", "online", "global",
    "secure", "service", "services", "group", "solutions", "consulting",
    "care", "health", "food", "bank", "market", "trade", "prime", "super",
})

# Small dictionary-common word set for the arbitrary bucket (real words a
# business might adopt that say nothing about typical goods).
_ARBITRARY_DICTIONARY_WORDS = frozenset({
    "apple", "orange", "tiger", "falcon", "amber", "delta", "shell", "dove",
    "camel", "arrow", "anchor", "canyon", "ember", "harbor", "atlas", "puma",
    "raven", "onyx", "maple", "polo",
})

_QUOTED_MARK_RE = re.compile(r"[\"“'‘]([A-Za-z][A-Za-z0-9 \-]{1,40})[\"”'’]")
_CALLED_MARK_RE = re.compile(r"\bmark\s+(?:called|named)\s+([A-Za-z][A-Za-z0-9\-]{1,40})")

_REGISTRY_BY_JURISDICTION = {"US": "USPTO", "EU": "EUIPO"}


def _extract_mark(context: dict) -> str | None:
    explicit = context.get("mark")
    if explicit:
        return str(explicit).strip()[:80]
    description = context.get("description", "") or ""
    match = _QUOTED_MARK_RE.search(description) or _CALLED_MARK_RE.search(description)
    return match.group(1).strip()[:80] if match else None


def _classify_distinctiveness(mark: str, goods_services: str) -> tuple[str, float, str]:
    """Deterministic Abercrombie-spectrum bucket → (category, confidence, reason)."""
    mark_norm = re.sub(r"[^a-z0-9]", "", mark.lower())
    goods_tokens = set(re.findall(r"[a-z]+", (goods_services or "").lower()))

    if mark_norm and mark_norm in goods_tokens:
        return ("generic", 0.20,
                "the mark is the common name of the goods/services themselves "
                "— not protectable as a trademark")

    components = sorted(
        w for w in (_DESCRIPTIVE_WORDS | goods_tokens)
        if len(w) >= 4 and w in mark_norm
    )
    if components:
        return ("descriptive", 0.40,
                f"built from common descriptive words ({', '.join(components)}) "
                "— weak inherent distinctiveness; secondary meaning likely needed")

    if mark_norm in _ARBITRARY_DICTIONARY_WORDS:
        return ("arbitrary", 0.80,
                "a real dictionary word unrelated to the goods/services — "
                "strong inherent distinctiveness")

    if any(len(t) >= 4 and mark_norm.startswith(t[:4]) for t in goods_tokens):
        return ("suggestive", 0.60,
                "hints at the goods/services without describing them — "
                "moderate inherent distinctiveness")

    return ("fanciful", 0.80,
            "a coined term with no dictionary meaning — strongest inherent "
            "distinctiveness")


async def _scan_portfolio(
    deps: WorkflowAgentDeps, mark: str, nice_classes: list[int]
) -> list[tuple[Mark, float]]:
    """difflib similarity scan of the org's own Mark rows (portfolio context)."""
    rows = (
        await deps.session.execute(
            select(Mark).where(Mark.organization_id == deps.organization_id)
        )
    ).scalars().all()
    mark_l = mark.lower()
    conflicts: list[tuple[Mark, float]] = []
    for row in rows:
        ratio = SequenceMatcher(None, mark_l, (row.name or "").lower()).ratio()
        shared_class = bool(set(nice_classes) & {int(c) for c in (row.nice_classes or [])})
        if ratio >= 0.75 or (shared_class and ratio >= 0.6):
            conflicts.append((row, ratio))
    return conflicts


@register_workflow_agent("trademark_clearance_reviewer")
async def trademark_clearance_reviewer(
    context: dict, step_config: dict, deps: WorkflowAgentDeps
) -> WorkflowAgentOutput:
    mark = _extract_mark(context)
    goods_services = str(context.get("goods_services") or "").strip()
    nice_classes = [
        int(c) for c in (context.get("nice_classes") or [])
        if str(c).lstrip("-").isdigit()
    ]
    jurisdictions = [str(j) for j in (context.get("jurisdictions") or ["US"])]

    citations: list[dict] = [
        {"type": "Playbook", "id": _PLAYBOOK_ID, "title": "Trademark Clearance Playbook"}
    ]

    if not mark:
        comment = (
            "Agent: preliminary review could not identify the proposed mark in "
            "the request — ask the requester, then re-run. A formal registry "
            "search and counsel sign-off remain mandatory regardless."
        )
        draft = (
            "No proposed mark could be extracted from this request. Please "
            "confirm the exact mark (word/wordmark) with the requester before "
            f"any assessment.\n\n{_MANDATORY_BANNER}"
        )
        return WorkflowAgentOutput(
            proposed_action="approve",
            comment=comment,
            confidence=0.2,
            drafted_response=draft,
            citations=citations,
        )

    category, confidence, reason = _classify_distinctiveness(mark, goods_services)
    conflicts = await _scan_portfolio(deps, mark, nice_classes)
    if conflicts:
        confidence = min(confidence, 0.35)
        for row, _ratio in conflicts:
            citations.append({"type": "Mark", "id": row.id, "title": row.name})

    # ── Clearance memo (deterministic; the IP lead edits before any use) ──
    classes_line = (
        ", ".join(str(c) for c in nice_classes)
        if nice_classes
        else "not specified — to be confirmed at the IP Lead review step"
    )
    jurisdiction_lines = [
        f"  - {j}: formal "
        f"{_REGISTRY_BY_JURISDICTION.get(j.upper(), 'WIPO Global Brand Database')} "
        "registry search required before filing or first use"
        for j in jurisdictions
    ]
    if conflicts:
        conflict_lines = [
            f"  - {row.name} ({row.status}; Nice classes "
            f"{', '.join(str(c) for c in (row.nice_classes or [])) or 'n/a'}; "
            f"similarity {ratio:.2f}) — confusingly similar to the proposed mark"
            for row, ratio in conflicts
        ]
    else:
        conflict_lines = ["  - none found in the internal portfolio"]

    draft = "\n".join(
        [
            "TRADEMARK CLEARANCE MEMO — preliminary assessment",
            "",
            f"Proposed mark: {mark}",
            f"Goods/services: {goods_services or 'not specified'}",
            f"Distinctiveness: {category.upper()} — {reason}.",
            f"Proposed Nice classes: {classes_line}",
            "Jurisdictions:",
            *jurisdiction_lines,
            "Internal portfolio conflicts:",
            *conflict_lines,
            "",
            _MANDATORY_BANNER,
        ]
    )
    comment = (
        f"Agent: preliminary clearance of '{mark}' — {category} on the "
        f"distinctiveness spectrum; {len(conflicts)} internal portfolio "
        "conflict(s). A formal registry search and counsel sign-off are "
        "still mandatory."
    )

    return WorkflowAgentOutput(
        proposed_action="approve",  # advancing to the IP lead IS the escalation
        comment=comment,
        confidence=confidence,
        drafted_response=draft,
        citations=citations,
        ontology_writes=[],  # the completion hook authors the graph
    )


@register_completion_hook("trademark_clearance")
async def record_cleared_mark(session, instance) -> None:
    """When the trademark_clearance ladder completes (IP lead + clearance
    decision approved), author the ontology: a PENDING ``Mark`` row for the
    proposed mark, a clearance-memo ``Document`` node, and a
    Document --CONCERNS--> Mark edge — so every future clearance run screens
    against this mark too (one brain).

    Idempotent: the Mark is keyed on (org, name), the Document on the
    instance-scoped storage_url, and ``add_edge`` upserts on edge identity.

    Documented gap: spawning a Matter for the filing on approval is deferred
    until the Matter module ships CRUD — today the memo Document carries
    ``owner_type='MATTER'`` with the intake ticket as owner so the re-parent
    is a field update, not a migration.
    """
    context = instance.context or {}
    mark_name = _extract_mark(context)
    if not mark_name:
        return  # nothing to record — humans capture the mark later

    nice_classes = [
        int(c) for c in (context.get("nice_classes") or [])
        if str(c).lstrip("-").isdigit()
    ]
    jurisdictions = [str(j) for j in (context.get("jurisdictions") or ["US"])]

    mark_row = (
        await session.execute(
            select(Mark).where(
                Mark.organization_id == instance.organization_id,
                func.lower(Mark.name) == mark_name.lower(),
            )
        )
    ).scalars().first()
    if mark_row is None:
        mark_row = Mark(
            organization_id=instance.organization_id,
            name=mark_name,
            nice_classes=nice_classes,
            jurisdictions=jurisdictions,
            status="PENDING",
        )
        session.add(mark_row)
        await session.flush()
        await log_audit(
            session,
            organization_id=instance.organization_id,
            actor_id=None,
            actor_type="SYSTEM",
            action="mark.created",
            resource_type="Mark",
            resource_id=mark_row.id,
            after_json={
                "name": mark_name,
                "nice_classes": nice_classes,
                "jurisdictions": jurisdictions,
                "status": "PENDING",
            },
            metadata={
                "source": "trademark_clearance completion",
                "workflow_instance_id": instance.id,
            },
        )

    storage_url = f"workflow://{instance.id}/clearance-memo"
    doc = (
        await session.execute(
            select(Document).where(
                Document.organization_id == instance.organization_id,
                Document.storage_url == storage_url,
            )
        )
    ).scalars().first()
    if doc is None:
        doc = Document(
            organization_id=instance.organization_id,
            name=f"Clearance memo — {mark_name}",
            mime_type="application/pdf",
            size_bytes=0,
            storage_url=storage_url,
            owner_type="MATTER",
            owner_id=instance.entity_id,
            uploaded_by=instance.started_by or "system",
            extracted_text=(
                f"Preliminary trademark clearance memo for proposed mark "
                f"{mark_name} (workflow {instance.id}). Portfolio-context "
                "assessment only — a formal registry search and counsel "
                "sign-off were mandatory gates in this ladder."
            ),
        )
        session.add(doc)
        await session.flush()

    await add_edge(
        session,
        organization_id=instance.organization_id,
        src=NodeRef("Document", doc.id),
        label="CONCERNS",
        dst=NodeRef("Mark", mark_row.id),
        properties={"workflow_instance_id": instance.id},
        source_module="matter",
        created_by=instance.started_by,
    )
