"""Pydantic v2 schemas — these ARE the API contract (surfaced at /docs)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

IntakeSource = Literal["FORM", "COPILOT", "EMAIL", "SLACK", "API", "TEAMS"]
IntakeStatus = Literal[
    "AWAITING_TRIAGE", "IN_REVIEW", "APPROVED", "REJECTED", "ESCALATED", "CLOSED"
]
RecommendationStatus = Literal["PENDING", "APPROVED", "EDITED", "REJECTED"]


class CreateTicketRequest(BaseModel):
    # Untrusted free text — length-bounded to blunt abuse.
    description: str = Field(min_length=3, max_length=10_000)
    requester_name: str = Field(min_length=1, max_length=200)
    requester_email: str | None = Field(default=None, max_length=320)
    department: str | None = Field(default=None, max_length=200)
    source: IntakeSource = "FORM"
    # Optional form-selected type; the classifier still runs.
    type_hint: str | None = Field(default=None, max_length=120)


class RecommendationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    ticket_id: str
    agent_id: str
    confidence: float
    suggested_action: str
    drafted_response: str
    reasoning: str
    concerns: list[str]
    citations: list[dict]
    short_form_reply: str | None
    status: RecommendationStatus
    reviewed_by: str | None
    reviewed_at: datetime | None
    created_at: datetime


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    requester_id: str
    matter_id: str | None
    source: IntakeSource
    type: str
    priority: str
    status: IntakeStatus
    stage: str
    description: str
    department: str | None
    assigned_to: str | None
    sla_hours: int
    sla_status: str
    ai_triage_json: dict | None
    triaged_by: str | None
    triaged_at: datetime | None
    triaged_action: str | None
    submitted_at: datetime
    created_at: datetime


class TicketWithRecommendation(BaseModel):
    ticket: TicketOut
    recommendation: RecommendationOut | None


class ApproveRequest(BaseModel):
    # Optional human edit to the drafted response before approval. When
    # present, the recommendation is recorded as EDITED (still a human
    # decision, still audited).
    edited_response: str | None = Field(default=None, max_length=20_000)


class RejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2_000)


class ChainVerificationOut(BaseModel):
    ok: bool
    rows_checked: int
    broken_at_position: int | None = None
    reason: str | None = None
    problems: list[str] = Field(default_factory=list)
