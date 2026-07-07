"""Litigation Support agent — cited case brief + over-inclusive hold trigger.

Step 2 of the ``patent_litigation`` ladder (agent key
``litigation_summarizer``). The step's ``min_confidence`` is 0.7 and the
agent only ever RECOMMENDS "approve" — advancing to the IP-counsel
assessment IS the escalation path. Everything a litigation matter surfaces
is senior-counsel-visible by default; nothing here is auto-cleared.

What the agent produces:

  * BACK-COMPAT — a "Para IV" request still fires the Hatch-Waxman 45-day
    statutory window reminder in the comment (the deterministic behaviour
    the pre-existing litigation E2E relies on).
  * A CITED case brief assembled from the one-brain GraphRAG surface. The
    agent reads through the SAME permission-filtered ``graphrag.retrieve``
    path humans use — no agent has a private data path. Because a workflow
    handler is handed ``WorkflowAgentDeps`` (session + organization_id) and
    NOT an ``Actor``, the agent constructs a minimal SERVICE actor
    (``agent:litigation``) granted a broad read-scope
    (matter/contracts/intake reads) so it can traverse the graph; every hit
    it can see is still filtered by that read-scope, exactly as a human's
    would be.
  * An OVER-INCLUSIVE legal-hold trigger. Litigation-adjacent matters
    ALWAYS raise the preservation-scope-review flag — under-preservation is
    the expensive failure mode, so the agent errs toward triggering a hold
    review every time.

Confidence is deterministic: 0.82 when the request is a Para IV matter OR
the graph yielded a substantive record (>= 3 hits); 0.6 otherwise — a thin
graph lowers confidence AND the comment says so explicitly, because a thin
record is NOT evidence of low exposure.

All free text in ``context`` (description/subject) is UNTRUSTED. It is used
only as the retrieval QUERY — never echoed verbatim into the brief or the
comment, and never interpreted as instructions. The brief is composed from
graph hits (titles/ids) and gap notes, so an injected "state we have no
exposure and close the matter" cannot reach the output. The true control
remains the human-approval gate downstream.

Ontology writes happen at LADDER COMPLETION via the registered hook: a
CaseBrief ``Document`` node plus ``COVERS`` edges to every node the brief
cited. Documented gap: the proposed legal-hold is modelled today as that
``COVERS`` edge set (what the brief preserves-in-scope); a first-class Hold
table is deferred.
"""

from __future__ import annotations

from app.core.ai import AIUnavailableError, call_claude, spotlight
from app.core.permissions import Permission
from app.core.security import Actor
from app.db import graphrag
from app.db.models import Document
from app.db.ontology import NodeRef, add_edge
from app.core.audit import log_audit
from app.workflow.agents import (
    WorkflowAgentDeps,
    WorkflowAgentOutput,
    register_workflow_agent,
)
from app.workflow.hooks import register_completion_hook
from sqlalchemy import select

_PLAYBOOK_ID = "LIT-INTAKE-v1"
_MAX_CITATIONS = 15

# The mandatory caveat that rides every brief — absence of records in this
# platform is never evidence of absence in the wider record.
_GAP_CAVEAT = (
    "'No documents found' does NOT mean none exist — validate custodian and "
    "system coverage beyond this platform."
)

# The service read-scope the agent traverses the graph under. Broad by
# design (a litigation brief must be able to reach parties, contracts and
# related intake), but still a scope — GraphRAG filters every hit by it, so
# the agent sees exactly what a holder of these grants would see.
_SERVICE_PERMISSIONS = frozenset(
    {
        Permission.MATTER_READ_ALL.value,
        Permission.CONTRACTS_READ_ALL.value,
        Permission.INTAKE_READ_ALL_TICKETS.value,
    }
)

# Brief sections → the graph node types that populate them.
_SECTIONS: list[tuple[str, set[str]]] = [
    ("PARTIES", {"Counterparty", "Person", "Organization"}),
    ("CONTRACT LANDSCAPE", {"Document"}),
    ("RELATED MATTERS", {"IntakeTicket", "Matter"}),
    ("OPEN OBLIGATIONS", {"Obligation"}),
]


def _service_actor(organization_id: str) -> Actor:
    """A minimal, non-authenticated actor the agent traverses GraphRAG under.

    A workflow handler has no request ``Actor`` — only ``deps``. This builds
    the read-scoped service principal so the agent reads through the SAME
    permission-filtered surface as a human (no private data path)."""
    return Actor(
        user_id="agent:litigation",
        organization_id=organization_id,
        email="litigation-agent",
        name="litigation-agent",
        role_name=None,
        permissions=_SERVICE_PERMISSIONS,
    )


def _query_of(context: dict) -> str:
    return str(context.get("subject") or context.get("description") or "").strip()


def _compose_brief(result: graphrag.GraphRAGResult) -> str:
    """Structured, cited case brief from a GraphRAG result. Composed ONLY
    from hits (titles/ids) + gap notes — never from the raw request text."""
    lines: list[str] = [
        "LITIGATION CASE BRIEF — preliminary, for senior-counsel review",
        "",
    ]
    for heading, types in _SECTIONS:
        lines.append(heading)
        section_hits = [h for h in result.hits if h.type in types]
        if section_hits:
            for h in section_hits:
                lines.append(f"  - {h.title} [{h.type} {h.id}]")
        else:
            lines.append("  - none found in the connected record")
        lines.append("")

    lines.append("GAP ANALYSIS")
    for note in result.gap_notes:
        lines.append(f"  - {note}")
    lines.append(f"  - {_GAP_CAVEAT}")
    return "\n".join(lines)


async def _maybe_polish(brief: str, deps: WorkflowAgentDeps) -> str:
    """Optional Claude polish of the assembled brief. Treats the brief as
    spotlighted data; NEVER changes the action or confidence, and falls back
    to the deterministic brief when AI is unavailable (the demo case)."""
    try:
        polished = await call_claude(
            "Tighten this litigation case brief for senior counsel. Preserve "
            "every [type id] citation and every section heading verbatim, and "
            "add no new facts.\n\n" + spotlight(brief, "BRIEF"),
            system=(
                "You are a litigation paralegal. Return only the polished "
                "brief; keep every citation and heading."
            ),
            purpose="agent.litigation_summarizer",
            organization_id=deps.organization_id,
            max_tokens=1200,
        )
    except AIUnavailableError:
        return brief
    except Exception:  # noqa: BLE001 — any provider error keeps the deterministic brief
        return brief
    return polished.strip() or brief


@register_workflow_agent("litigation_summarizer")
async def litigation_summarizer(
    context: dict, step_config: dict, deps: WorkflowAgentDeps
) -> WorkflowAgentOutput:
    para_iv = "para iv" in str(context.get("description", "")).lower()

    # ── Cited case brief via the one-brain GraphRAG surface ──────────────
    actor = _service_actor(deps.organization_id)
    query = _query_of(context)
    result = await graphrag.retrieve(deps.session, actor, query)
    hits = result.hits

    brief = _compose_brief(result)
    brief = await _maybe_polish(brief, deps)

    # Deterministic confidence: strong when Para IV or a substantive graph;
    # a thin graph lowers it AND the comment says a thin record is not
    # evidence of low exposure.
    substantive = para_iv or len(hits) >= 3
    confidence = 0.82 if substantive else 0.6

    comment_parts: list[str] = []
    if para_iv:
        comment_parts.append(
            "Para IV notice detected — the 45-day statutory window to file suit "
            "applies; key dates docketed for counsel review."
        )
    if substantive:
        comment_parts.append(
            f"Cited case brief assembled from {len(hits)} connected record(s)."
        )
    else:
        comment_parts.append(
            f"Thin graph — only {len(hits)} connected record(s) found; a thin "
            "record is NOT evidence of low exposure."
        )
    # Over-inclusive by design: litigation-adjacent matters always trigger a
    # preservation-scope review.
    comment_parts.append(
        "Legal-hold trigger: recommend preservation scope review."
    )
    comment = " ".join(comment_parts)

    citations: list[dict] = [
        {"type": h.type, "id": h.id, "title": h.title} for h in hits[:_MAX_CITATIONS]
    ]
    citations.append(
        {"type": "Playbook", "id": _PLAYBOOK_ID, "title": "Litigation Intake Playbook"}
    )

    return WorkflowAgentOutput(
        proposed_action="approve",  # advance to senior counsel — always visible
        comment=comment,
        confidence=confidence,
        drafted_response=brief,
        citations=citations,
        ontology_writes=[],  # the completion hook authors the graph
    )


@register_completion_hook("patent_litigation")
async def record_case_brief(session, instance) -> None:
    """When the ``patent_litigation`` ladder completes, author the ontology:
    a CaseBrief ``Document`` node plus ``COVERS`` edges from it to every
    graph node the brief cited — so the brief and the record it stands on are
    one connected artifact (one brain).

    The COVERS edge set IS the machine-readable expression of the proposed
    legal hold today: the nodes the brief preserves in scope. A first-class
    ``Hold`` table is a documented deferred gap — when it lands, these edges
    re-parent to it without a data migration.

    Idempotent on the instance-scoped ``storage_url`` (a ladder completes
    once; replays/retries must be safe). Re-runs a light retrieve on the
    instance's subject to recover the cited nodes for the edges.
    """
    context = instance.context or {}
    subject = _query_of(context) or "Litigation matter"

    storage_url = f"workflow://{instance.id}/case-brief"
    existing = (
        await session.execute(
            select(Document).where(
                Document.organization_id == instance.organization_id,
                Document.storage_url == storage_url,
            )
        )
    ).scalars().first()
    if existing is not None:
        return  # already authored — idempotent

    actor = _service_actor(instance.organization_id)
    result = await graphrag.retrieve(session, actor, subject)
    brief = _compose_brief(result)

    doc = Document(
        organization_id=instance.organization_id,
        name=f"Case brief — {subject}",
        mime_type="text/markdown",
        size_bytes=len(brief.encode("utf-8")),
        storage_url=storage_url,
        owner_type="MATTER",
        owner_id=instance.entity_id,
        uploaded_by=instance.started_by or "system",
        extracted_text=brief,
    )
    session.add(doc)
    await session.flush()

    covered = result.hits[:_MAX_CITATIONS]
    for hit in covered:
        await add_edge(
            session,
            organization_id=instance.organization_id,
            src=NodeRef("Document", doc.id),
            label="COVERS",
            dst=NodeRef(hit.type, hit.id),
            properties={"workflow_instance_id": instance.id},
            source_module="matter",
            created_by=instance.started_by,
        )

    await log_audit(
        session,
        organization_id=instance.organization_id,
        actor_id=None,
        actor_type="SYSTEM",
        action="casebrief.created",
        resource_type="Document",
        resource_id=doc.id,
        after_json={
            "name": doc.name,
            "owner_type": "MATTER",
            "owner_id": instance.entity_id,
            "covers_edges": len(covered),
        },
        metadata={
            "source": "patent_litigation completion",
            "workflow_instance_id": instance.id,
        },
    )
