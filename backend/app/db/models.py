"""SQLAlchemy models — shared entities + Legal Intake.

Shared entities (Organization, Role, User, Person, Counterparty, Document,
Obligation, Event, Tag, Tagging, AuditLog) live here ONCE. Every module
attaches to them; modules never re-implement a parallel party/contact/file
table. This is the "one brain" differentiator at the schema level.

Column names are snake_case (idiomatic Postgres/Python). Enums are stored as
strings and validated in the Pydantic schema layer + Python enums, keeping
migrations simple and additive.

The AuditLog table's ``prev_hash`` / ``content_hash`` / ``chain_position``
columns are filled by Postgres triggers (see migration 0002) — application
code never sets them.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import ARRAY
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.ids import gen_id
from app.db.base import Base


def _pk() -> Mapped[str]:
    return mapped_column(String, primary_key=True, default=gen_id)


class Organization(Base):
    __tablename__ = "organization"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String, nullable=False)
    tier: Mapped[str] = mapped_column(String, nullable=False, default="DEMO")
    region: Mapped[str] = mapped_column(String, nullable=False, default="US")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    roles: Mapped[list["Role"]] = relationship(back_populates="organization")
    users: Mapped[list["User"]] = relationship(back_populates="organization")


class Role(Base):
    __tablename__ = "role"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    # Flat array of permission strings (the enum VALUES from permissions.py).
    permissions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    organization: Mapped[Organization] = relationship(back_populates="roles")
    users: Mapped[list["User"]] = relationship(back_populates="role")


class User(Base):
    # "user" is effectively reserved in Postgres — use app_user.
    __tablename__ = "app_user"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    role_id: Mapped[str | None] = mapped_column(
        ForeignKey("role.id", ondelete="SET NULL"), nullable=True, index=True
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Soft delete: row stays so AuditLog actor_id references resolve, but a
    # suspended user cannot authenticate.
    suspended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    organization: Mapped[Organization] = relationship(back_populates="users")
    role: Mapped[Role | None] = relationship(back_populates="users")


class Counterparty(Base):
    __tablename__ = "counterparty"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    # COMPANY | INDIVIDUAL | LAW_FIRM | REGULATOR | OTHER
    type: Mapped[str] = mapped_column(String, nullable=False)
    country: Mapped[str | None] = mapped_column(String, nullable=True)
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("counterparty.id", ondelete="SET NULL"), nullable=True
    )
    sanctions_screened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    payload_metadata: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Person(Base):
    __tablename__ = "person"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    # EMPLOYEE | EXTERNAL_COUNSEL | CUSTODIAN | DATA_SUBJECT | COUNTERPARTY_CONTACT
    type: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True, index=True
    )
    external_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    payload_metadata: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Document(Base):
    __tablename__ = "document"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    mime_type: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_url: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    parent_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("document.id", ondelete="SET NULL"), nullable=True
    )
    # MATTER | CONTRACT | DSAR | COMPLIANCE | BOARD | INTAKE
    owner_type: Mapped[str] = mapped_column(String, nullable=False)
    owner_id: Mapped[str] = mapped_column(String, nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Demo-grade inline extraction; treated as UNTRUSTED input by agents.
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)


class Obligation(Base):
    __tablename__ = "obligation"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    # CONTRACT | REGULATION | POLICY | PRIVACY_LAW
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    source_id: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    due_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recurrence: Mapped[str | None] = mapped_column(String, nullable=True)
    owner_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # OPEN | IN_PROGRESS | MET | BREACHED | WAIVED
    status: Mapped[str] = mapped_column(String, nullable=False, default="OPEN")
    payload_metadata: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Event(Base):
    __tablename__ = "event"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String, nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    source_id: Mapped[str] = mapped_column(String, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    indexed_for_search: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )


class Tag(Base):
    __tablename__ = "tag"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    color: Mapped[str] = mapped_column(String, nullable=False)


class Tagging(Base):
    __tablename__ = "tagging"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    tag_id: Mapped[str] = mapped_column(
        ForeignKey("tag.id", ondelete="CASCADE"), index=True
    )
    tagged_type: Mapped[str] = mapped_column(String, nullable=False)
    tagged_id: Mapped[str] = mapped_column(String, nullable=False)


class AuditLog(Base):
    """Append-only, hash-chained ledger. See migration 0002 for the triggers.

    ``prev_hash`` / ``content_hash`` / ``chain_position`` are filled by the
    BEFORE INSERT trigger — application code must never set them, and UPDATE
    / DELETE are blocked at the database level.
    """

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    actor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # USER | AGENT | SYSTEM
    actor_type: Mapped[str] = mapped_column(String, nullable=False, default="USER")
    action: Mapped[str] = mapped_column(String, nullable=False)
    resource_type: Mapped[str] = mapped_column(String, nullable=False)
    resource_id: Mapped[str] = mapped_column(String, nullable=False)
    before_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    payload_metadata: Mapped[dict | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )

    # Trigger-managed chain columns.
    prev_hash: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    content_hash: Mapped[str] = mapped_column(
        String, nullable=False, server_default=""
    )
    chain_position: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )

    # Declared here so Alembic autogenerate knows the index exists — it is
    # CREATED by migration 0002 (after the chain backfill) and must never be
    # dropped: a duplicate (org, chain_position) means a trigger bypass.
    __table_args__ = (
        Index(
            "uq_audit_log_org_chain_position",
            "organization_id",
            "chain_position",
            unique=True,
        ),
    )


# ══════════════════════════════════════════════════════════════════════
# Ontology + AI governance (PR 2)
# ══════════════════════════════════════════════════════════════════════


class OntologyEmbedding(Base):
    """Vector embedding of a shared-entity node — the GraphRAG vector leg.

    Stored as a portable ``float8[]`` (Postgres array) so the schema applies
    everywhere, including sandboxes without the ``vector`` extension.
    Cosine reranking runs in Python over the graph-narrowed candidate set
    (already small), so no ANN index is required for correctness. Where
    pgvector IS available (Neon), the optional follow-up migration adds a
    ``vector`` column + HNSW index for fast GLOBAL search — this row stays
    the source of truth either way.

    One row per (org, node); ``content_hash`` lets the indexer skip
    re-embedding unchanged nodes. ``model`` records which model produced the
    vector so a model swap can be detected and reindexed.
    """

    __tablename__ = "ontology_embedding"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    node_type: Mapped[str] = mapped_column(String, nullable=False)
    node_id: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list] = mapped_column(ARRAY(Float), nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "node_type", "node_id",
                         name="uq_ontology_embedding_node"),
    )


class OntologyEdge(Base):
    """A typed link between two shared-entity nodes — the ontology's edges.

    Nodes are the shared entities themselves (Counterparty, Person, Document,
    Contract, Obligation, Matter, …) addressed polymorphically by
    (type, id). Edges are AUTHORED by modules as a byproduct of normal legal
    work — never extracted by an LLM — so every edge is accurate, cheap, and
    permissioned at birth. Labels are dot-free UPPER_SNAKE verbs:
    PARTY_TO, NDA_WITH, OBLIGATES, DUE, CITES, COVERS, SCREENED_ON,
    SAME_AS, PARENT_OF, …
    """

    __tablename__ = "ontology_edge"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    src_type: Mapped[str] = mapped_column(String, nullable=False)
    src_id: Mapped[str] = mapped_column(String, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    dst_type: Mapped[str] = mapped_column(String, nullable=False)
    dst_id: Mapped[str] = mapped_column(String, nullable=False)
    # Edge properties (e.g. NDA_WITH carries term + expiry).
    properties: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Which module authored the edge (provenance, not authorization).
    source_module: Mapped[str] = mapped_column(String, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "src_type", "src_id", "label", "dst_type", "dst_id",
            name="uq_ontology_edge_identity",
        ),
        Index("ix_ontology_edge_src", "organization_id", "src_type", "src_id"),
        Index("ix_ontology_edge_dst", "organization_id", "dst_type", "dst_id"),
        Index("ix_ontology_edge_label", "organization_id", "label"),
    )


class AgentDecision(Base):
    """The platform-wide conservative-AI contract.

    Every agent recommendation that would mutate state lands here PENDING.
    The ONLY paths out are the human approve call (APPROVED, or
    APPROVED_WITH_OVERRIDE when the human edited the payload) — which
    executes the governed action and writes the audit row in the same
    transaction — or the human reject call. Downstream mutations gate on an
    APPROVED status; the gate is schema + transaction, not prompt.
    """

    __tablename__ = "agent_decision"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    # The resource the recommendation concerns (polymorphic).
    resource_type: Mapped[str] = mapped_column(String, nullable=False)
    resource_id: Mapped[str] = mapped_column(String, nullable=False)
    # Governed action executed on approval — a key in the action registry
    # (core/governance.py), e.g. "intake.send_response", "matter.spawn".
    action_key: Mapped[str] = mapped_column(String, nullable=False)
    action_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # The agent's full recommendation surface for the Cockpit:
    # {confidence, suggested_action, drafted_response, reasoning,
    #  concerns[], citations[{type,id,title}], degraded}.
    recommendation: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # PENDING | APPROVED | APPROVED_WITH_OVERRIDE | REJECTED
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="PENDING", index=True
    )
    decided_by: Mapped[str | None] = mapped_column(String, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Audit row written when the approved action executed.
    executed_audit_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_agent_decision_resource", "organization_id", "resource_type", "resource_id"),
        Index("ix_agent_decision_org_status", "organization_id", "status"),
    )


class AICallLog(Base):
    """Telemetry for every model call through core/ai.py.

    Distinct from the audit chain: the chain records governed state changes;
    this ledger records model usage (cost, latency, failure) per org and
    purpose so AI operations are observable and budgetable. Best-effort —
    a telemetry failure never fails the call.
    """

    __tablename__ = "ai_call_log"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str | None] = mapped_column(String, nullable=True)
    purpose: Mapped[str] = mapped_column(String, nullable=False, default="general")
    model: Mapped[str] = mapped_column(String, nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    response_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_ai_call_log_org_created", "organization_id", "created_at"),
    )


# ══════════════════════════════════════════════════════════════════════
# Reference + module data for the agent fleet (PRs 10–14)
# ══════════════════════════════════════════════════════════════════════


class SanctionsEntry(Base):
    """Curated sanctions/debarment reference list (global — not org data).

    Demo rows ship in the seed; the OFAC SDN feed loader (PR 10 follow-up
    job) upserts into this same table keyed by (list_source, name). Lists
    change daily — screening results carry the list version they matched.
    """

    __tablename__ = "sanctions_entry"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    list_source: Mapped[str] = mapped_column(String, nullable=False)  # e.g. OFAC_SDN
    list_version: Mapped[str] = mapped_column(String, nullable=False, default="demo-1")
    name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    aliases: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    country: Mapped[str | None] = mapped_column(String, nullable=True)
    program: Mapped[str | None] = mapped_column(String, nullable=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("list_source", "name", name="uq_sanctions_entry_source_name"),
    )


class KnowledgeEntry(Base):
    """Versioned, approved knowledge — the ONLY corpus the FAQ and Policy
    agents may quote. Every save is a new version row; exactly one
    is_current per (org, kind, slug). Answers cite (entry, version)."""

    __tablename__ = "knowledge_entry"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)  # FAQ | POLICY
    slug: Mapped[str] = mapped_column(String, nullable=False)
    topic: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    owner_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    effective_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    review_due: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "kind", "slug", "version",
                         name="uq_knowledge_entry_slug_version"),
        Index("ix_knowledge_entry_current", "organization_id", "kind", "is_current"),
    )


class Mark(Base):
    """The org's trademark portfolio — what the Trademark agent screens
    proposed marks against (portfolio context, NOT a registry search)."""

    __tablename__ = "mark"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    nice_classes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    jurisdictions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # REGISTERED | PENDING | ABANDONED
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_mark_org_name"),
    )


class Assessment(Base):
    """Privacy / DPIA assessment record (Data-Privacy agent, PR 16).

    Authored on approval of the data_breach / privacy ladder. The
    processing map (System -> DataCategory) is authored as ontology edges;
    this row holds the rating + lawful-basis + conditions summary."""

    __tablename__ = "assessment"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    subject: Mapped[str] = mapped_column(String, nullable=False)  # system/vendor/initiative
    # LOW | MEDIUM | HIGH
    risk_rating: Mapped[str] = mapped_column(String, nullable=False, default="MEDIUM")
    lawful_basis: Mapped[str | None] = mapped_column(String, nullable=True)
    data_categories: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    cross_border: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    conditions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    gaps: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    source_workflow_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ClaimEntry(Base):
    """Approved marketing-claim library entry (Marketing agent, PR 17).

    A claim cleared for a product/market with substantiation + expiry.
    Review compares submitted copy against these; verbatim reuse is
    fast-track-eligible, any deviation is a new (pending) claim."""

    __tablename__ = "claim_entry"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    product: Mapped[str] = mapped_column(String, nullable=False)
    market: Mapped[str] = mapped_column(String, nullable=False, default="US")
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    substantiation_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    # True = regulated product/therapeutic claim → mandatory human review.
    regulated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # APPROVED | PENDING | EXPIRED
    status: Mapped[str] = mapped_column(String, nullable=False, default="APPROVED")
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_claim_entry_product", "organization_id", "product", "market"),
    )


class ContractPlaybook(Base):
    """Per-type contract playbook (Contract-Type Specialist, PR 19).

    Counsel-owned, versioned. The router selects the playbook from the
    ticket's contract type; the specialist agent benchmarks against its
    mandatory / forbidden / negotiable bands. One configurable agent, many
    playbooks — no codebase per type."""

    __tablename__ = "contract_playbook"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    contract_type: Mapped[str] = mapped_column(String, nullable=False)  # vendor|services|...
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    mandatory_clauses: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    forbidden_clauses: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    negotiable_bands: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    owner_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "contract_type", "version",
                         name="uq_contract_playbook_type_version"),
    )


# ══════════════════════════════════════════════════════════════════════
# Workflow engine (shared package) — governance ladders
# ══════════════════════════════════════════════════════════════════════
#
# Ported from the reference dynamic workflow engine, org-scoped and
# reconciled with the non-negotiables: every transition twin-records to the
# hash-chained audit_log, and agent steps emit PENDING AgentDecisions —
# a human approval is what advances the ladder.


class WorkflowDefinition(Base):
    __tablename__ = "workflow_definition"
    __table_args__ = (
        UniqueConstraint("organization_id", "key", "version",
                         name="uq_workflow_definition_org_key_version"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(String, nullable=False)
    # Definitions are versioned; in-flight instances pin their definition row
    # so an admin edit (a new version) never mutates a running ladder.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    steps: Mapped[list["WorkflowStep"]] = relationship(
        back_populates="definition",
        order_by="WorkflowStep.step_order",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class WorkflowStep(Base):
    __tablename__ = "workflow_step"
    __table_args__ = (
        UniqueConstraint("definition_id", "step_order",
                         name="uq_workflow_step_definition_order"),
        CheckConstraint("step_order BETWEEN 1 AND 15",
                        name="workflow_step_order_range"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    definition_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_definition.id", ondelete="CASCADE"), index=True
    )
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # Links the step to an existing app screen (frontend screenRegistry).
    screen_key: Mapped[str] = mapped_column(String, nullable=False)
    approver_role: Mapped[str | None] = mapped_column(String, nullable=True)
    # human | agent — agent steps run a registered handler whose output
    # becomes a PENDING AgentDecision (never auto-applied).
    kind: Mapped[str] = mapped_column(String, nullable=False, default="human")
    agent_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    sla_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # {"skip_if": {"field": ..., "op": ..., "value": ...}}
    payload_metadata: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )

    definition: Mapped[WorkflowDefinition] = relationship(back_populates="steps")


class WorkflowInstance(Base):
    __tablename__ = "workflow_instance"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    definition_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_definition.id"), index=True
    )
    # Polymorphic subject, e.g. ("IntakeTicket", "REQ-3501").
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[str] = mapped_column(String, nullable=False)
    current_step_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # in_progress | completed | cancelled
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="in_progress", index=True
    )
    started_by: Mapped[str | None] = mapped_column(String, nullable=True)
    context: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Optimistic lock — two approvers acting at once can't both win.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    step_entered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    definition: Mapped[WorkflowDefinition] = relationship(lazy="selectin")
    transitions: Mapped[list["WorkflowTransition"]] = relationship(
        back_populates="instance",
        order_by="WorkflowTransition.created_at",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_workflow_instance_entity",
              "organization_id", "entity_type", "entity_id"),
    )


class WorkflowTransition(Base):
    """Product-surface trail of ladder movement. The compliance record is the
    twin audit_log row written in the same transaction (workflow.instance.*)."""

    __tablename__ = "workflow_transition"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    instance_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_instance.id", ondelete="CASCADE"), index=True
    )
    from_step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    to_step_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # start | approve | reject | send_back | cancel
    action: Mapped[str] = mapped_column(String, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Display label, e.g. "agent:nda_reviewer (approved by Alex Nguyen)".
    actor_label: Mapped[str] = mapped_column(String, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    instance: Mapped[WorkflowInstance] = relationship(back_populates="transitions")


class WorkflowAgentTask(Base):
    """One agent-step visit. Terminal states: awaiting_approval (a PENDING
    AgentDecision carries the proposed action), done (decision decided),
    failed (handler error — human acts manually)."""

    __tablename__ = "workflow_agent_task"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    instance_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_instance.id", ondelete="CASCADE"), index=True
    )
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    # pending | awaiting_approval | done | failed
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    input: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    output: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    decision_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# ══════════════════════════════════════════════════════════════════════
# Legal Intake module tables
# ══════════════════════════════════════════════════════════════════════


class IntakeTicket(Base):
    __tablename__ = "intake_ticket"

    # Human-readable id, e.g. "REQ-3501" — not a cuid.
    id: Mapped[str] = mapped_column(String, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    requester_id: Mapped[str] = mapped_column(
        ForeignKey("person.id", ondelete="CASCADE"), index=True
    )
    matter_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # FORM | COPILOT | EMAIL | SLACK | API | TEAMS
    source: Mapped[str] = mapped_column(String, nullable=False, default="FORM")

    type: Mapped[str] = mapped_column(String, nullable=False)
    priority: Mapped[str] = mapped_column(String, nullable=False)
    # AWAITING_TRIAGE | IN_REVIEW | APPROVED | REJECTED | ESCALATED | CLOSED
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="AWAITING_TRIAGE", index=True
    )
    stage: Mapped[str] = mapped_column(String, nullable=False, default="new")
    description: Mapped[str] = mapped_column(Text, nullable=False)
    department: Mapped[str | None] = mapped_column(String, nullable=True)
    assigned_to: Mapped[str | None] = mapped_column(String, nullable=True)
    assigned_to_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )

    sla_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    sla_status: Mapped[str] = mapped_column(String, nullable=False, default="On Track")

    ai_triage_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    triaged_by: Mapped[str | None] = mapped_column(String, nullable=True)
    triaged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    triaged_action: Mapped[str | None] = mapped_column(String, nullable=True)
    agent_processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Inbound-channel dedup: the source message's stable id (email
    # internetMessageId, webhook messageId). A retry/replay of the same
    # message resolves to the existing ticket instead of duplicating.
    # NULLs are distinct in Postgres, so FORM/COPILOT tickets are unaffected.
    external_message_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # The governance ladder driving this ticket (Front Door routing).
    workflow_instance_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflow_instance.id", ondelete="SET NULL"), nullable=True
    )

    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    recommendations: Mapped[list["AgentRecommendation"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "external_message_id",
                         name="uq_intake_ticket_org_external_message"),
    )


class AgentRecommendation(Base):
    """An agent's RECOMMENDATION. Persisted PENDING; the only path to
    APPROVED is a human approve call, which also writes the audit row."""

    __tablename__ = "agent_recommendation"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("intake_ticket.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    suggested_action: Mapped[str] = mapped_column(String, nullable=False)
    drafted_response: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reasoning: Mapped[str] = mapped_column(Text, nullable=False, default="")
    concerns: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    citations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    short_form_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    # PENDING | APPROVED | EDITED | REJECTED
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="PENDING", index=True
    )
    reviewed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    ticket: Mapped[IntakeTicket] = relationship(back_populates="recommendations")
