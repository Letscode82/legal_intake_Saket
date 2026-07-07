"""Canonical permission catalog + role bundles + authorization check.

Ported verbatim from the frontend's `@aegis/auth` package (permissions.ts +
roles.ts) so the two deployables share one permission vocabulary. The enum
VALUES are the stable contract — they are the exact strings persisted in
``role.permissions`` JSON. Renaming a value is a breaking change; add new
values, never repurpose existing ones.

RBAC is enforced authoritatively in THIS backend on every mutation (via the
``require_permission`` dependency). The frontend may hide affordances a user
lacks, but the backend is the only authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Permission(str, Enum):
    # ── Intake ───────────────────────────────────────────────────────
    INTAKE_CREATE_TICKET = "intake:create_ticket"
    INTAKE_READ_OWN_TICKETS = "intake:read_own_tickets"
    INTAKE_READ_ALL_TICKETS = "intake:read_all_tickets"
    INTAKE_APPROVE_RECOMMENDATION = "intake:approve_recommendation"
    INTAKE_REJECT_RECOMMENDATION = "intake:reject_recommendation"
    INTAKE_CLOSE_TICKET = "intake:close_ticket"

    # ── Matter Management ────────────────────────────────────────────
    MATTER_READ_ALL = "matter:read_all"
    MATTER_READ_ASSIGNED = "matter:read_assigned"
    MATTER_CREATE = "matter:create"
    MATTER_UPDATE = "matter:update"
    MATTER_CLOSE = "matter:close"
    MATTER_LEGAL_HOLD_ISSUE = "matter:legal_hold:issue"
    MATTER_LEGAL_HOLD_RELEASE = "matter:legal_hold:release"
    MATTER_LEGAL_HOLD_CUSTODIAN_VIEW = "matter:legal_hold:custodian_view"

    # ── Contracts ────────────────────────────────────────────────────
    CONTRACTS_READ_ALL = "contracts:read_all"
    CONTRACTS_CREATE = "contracts:create"
    CONTRACTS_APPROVE = "contracts:approve"
    CONTRACTS_EXECUTE = "contracts:execute"

    # ── Spend & Counsel ──────────────────────────────────────────────
    SPEND_READ_ALL = "spend:read_all"
    SPEND_READ_MATTER_BUDGET = "spend:read_matter_budget"
    SPEND_APPROVE_INVOICE = "spend:approve_invoice"
    SPEND_REJECT_INVOICE = "spend:reject_invoice"

    # ── Privacy & Compliance Operations ──────────────────────────────
    PRIVACY_DSAR_READ = "privacy:dsar:read"
    PRIVACY_DSAR_FULFILL = "privacy:dsar:fulfill"
    PRIVACY_DPIA_READ = "privacy:dpia:read"
    PRIVACY_DPIA_APPROVE = "privacy:dpia:approve"
    PRIVACY_INCIDENT_RESPOND = "privacy:incident:respond"

    # ── Knowledge Management ─────────────────────────────────────────
    KNOWLEDGE_READ_ALL = "knowledge:read_all"
    KNOWLEDGE_CONTRIBUTE = "knowledge:contribute"
    KNOWLEDGE_MODERATE = "knowledge:moderate"

    # ── Regulatory Compliance ────────────────────────────────────────
    REGULATORY_READ = "regulatory:read"
    REGULATORY_FLAG_OBLIGATION = "regulatory:flag_obligation"

    # ── Governance ───────────────────────────────────────────────────
    GOVERNANCE_READ = "governance:read"
    GOVERNANCE_ATTEST = "governance:attest"

    # ── Audit ────────────────────────────────────────────────────────
    AUDIT_READ_ALL = "audit:read_all"

    # ── Admin ────────────────────────────────────────────────────────
    ADMIN_MANAGE_USERS = "admin:manage_users"
    ADMIN_MANAGE_ROLES = "admin:manage_roles"
    ADMIN_LEGAL_HOLD_TEMPLATES_MANAGE = "admin:legal_hold:templates_manage"
    ADMIN_M365_MANAGE = "admin:m365:manage"


ALL_PERMISSIONS: tuple[Permission, ...] = tuple(Permission)

RoleName = str

ALL_ROLES: tuple[str, ...] = (
    "admin",
    "gc",
    "attorney",
    "paralegal",
    "legal_ops",
    "requester",
    "external_counsel",
    "viewer",
)

# ── Composable permission bundles (mirror roles.ts) ──────────────────
_READ_ALL_BUNDLE: tuple[Permission, ...] = (
    Permission.INTAKE_READ_ALL_TICKETS,
    Permission.MATTER_READ_ALL,
    Permission.CONTRACTS_READ_ALL,
    Permission.SPEND_READ_ALL,
    Permission.SPEND_READ_MATTER_BUDGET,
    Permission.PRIVACY_DSAR_READ,
    Permission.PRIVACY_DPIA_READ,
    Permission.KNOWLEDGE_READ_ALL,
    Permission.REGULATORY_READ,
    Permission.GOVERNANCE_READ,
)

_ATTORNEY_WRITE_BUNDLE: tuple[Permission, ...] = (
    Permission.INTAKE_APPROVE_RECOMMENDATION,
    Permission.INTAKE_REJECT_RECOMMENDATION,
    Permission.INTAKE_CLOSE_TICKET,
    Permission.MATTER_CREATE,
    Permission.MATTER_UPDATE,
    Permission.MATTER_CLOSE,
    Permission.MATTER_LEGAL_HOLD_ISSUE,
    Permission.MATTER_LEGAL_HOLD_RELEASE,
    Permission.CONTRACTS_CREATE,
    Permission.CONTRACTS_APPROVE,
    Permission.SPEND_APPROVE_INVOICE,
    Permission.SPEND_REJECT_INVOICE,
    Permission.PRIVACY_DSAR_FULFILL,
)


def _dedup(*groups: tuple[Permission, ...]) -> tuple[Permission, ...]:
    seen: dict[Permission, None] = {}
    for group in groups:
        for perm in group:
            seen.setdefault(perm, None)
    return tuple(seen.keys())


ROLE_PERMISSIONS: dict[str, tuple[Permission, ...]] = {
    # admin is the superuser bundle — every Permission.
    "admin": tuple(Permission),
    "gc": _dedup(
        _READ_ALL_BUNDLE,
        _ATTORNEY_WRITE_BUNDLE,
        (
            Permission.MATTER_LEGAL_HOLD_CUSTODIAN_VIEW,
            Permission.PRIVACY_DPIA_APPROVE,
            Permission.PRIVACY_INCIDENT_RESPOND,
            Permission.GOVERNANCE_ATTEST,
            Permission.REGULATORY_FLAG_OBLIGATION,
            Permission.AUDIT_READ_ALL,
            Permission.ADMIN_MANAGE_USERS,
        ),
    ),
    "attorney": _dedup(
        (
            Permission.INTAKE_READ_ALL_TICKETS,
            Permission.INTAKE_READ_OWN_TICKETS,
            Permission.MATTER_READ_ASSIGNED,
            Permission.CONTRACTS_READ_ALL,
            Permission.SPEND_READ_MATTER_BUDGET,
            Permission.PRIVACY_DSAR_READ,
            Permission.KNOWLEDGE_READ_ALL,
            Permission.REGULATORY_READ,
            Permission.GOVERNANCE_READ,
        ),
        _ATTORNEY_WRITE_BUNDLE,
        (Permission.MATTER_LEGAL_HOLD_CUSTODIAN_VIEW,),
    ),
    "paralegal": _dedup(
        _READ_ALL_BUNDLE,
        (
            Permission.INTAKE_CREATE_TICKET,
            Permission.INTAKE_APPROVE_RECOMMENDATION,
            Permission.INTAKE_REJECT_RECOMMENDATION,
            Permission.INTAKE_CLOSE_TICKET,
            Permission.MATTER_CREATE,
            Permission.MATTER_UPDATE,
            Permission.MATTER_LEGAL_HOLD_CUSTODIAN_VIEW,
            Permission.KNOWLEDGE_CONTRIBUTE,
        ),
    ),
    "legal_ops": _dedup(
        _READ_ALL_BUNDLE,
        (
            Permission.INTAKE_CLOSE_TICKET,
            Permission.AUDIT_READ_ALL,
            Permission.SPEND_READ_MATTER_BUDGET,
            Permission.KNOWLEDGE_CONTRIBUTE,
            Permission.GOVERNANCE_ATTEST,
        ),
    ),
    "requester": (
        Permission.INTAKE_CREATE_TICKET,
        Permission.INTAKE_READ_OWN_TICKETS,
    ),
    "external_counsel": (
        Permission.MATTER_READ_ASSIGNED,
        Permission.MATTER_LEGAL_HOLD_CUSTODIAN_VIEW,
        Permission.SPEND_READ_MATTER_BUDGET,
    ),
    "viewer": tuple(_READ_ALL_BUNDLE),
}

# Sanity gate: admin must be the full permission set (mirrors roles.ts).
assert len(ROLE_PERMISSIONS["admin"]) == len(ALL_PERMISSIONS), (
    "admin role must include every Permission — found "
    f"{len(ROLE_PERMISSIONS['admin'])}, expected {len(ALL_PERMISSIONS)}."
)


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: str
    message: str


def can_user_do(
    permissions: set[str],
    permission: Permission,
    *,
    scope_ok: bool | None = None,
) -> AccessDecision:
    """Two-layer check: action grant + optional resource-scope predicate.

    ``permissions`` is the flattened set of permission strings the actor's
    role carries. ``scope_ok`` is supplied by the caller for the four
    resource-scoped permissions (matter assignment, ticket ownership,
    custodian membership); leave it None for org-scoped permissions.
    """
    if permission.value not in permissions:
        return AccessDecision(
            allowed=False,
            reason="missing_permission",
            message=f"Missing permission {permission.value}.",
        )
    if scope_ok is False:
        return AccessDecision(
            allowed=False,
            reason="out_of_scope",
            message=f"Permission {permission.value} granted but resource is out of scope.",
        )
    return AccessDecision(allowed=True, reason="ok", message="Allowed.")
