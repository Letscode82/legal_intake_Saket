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
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
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
