"""Permission catalog + role bundle invariants (pure unit — no DB)."""

from __future__ import annotations

from app.core.permissions import (
    ALL_PERMISSIONS,
    ALL_ROLES,
    ROLE_PERMISSIONS,
    Permission,
    can_user_do,
)


def test_admin_is_superuser_bundle():
    assert len(ROLE_PERMISSIONS["admin"]) == len(ALL_PERMISSIONS)
    assert set(ROLE_PERMISSIONS["admin"]) == set(Permission)


def test_every_role_has_a_bundle():
    for role in ALL_ROLES:
        assert role in ROLE_PERMISSIONS


def test_requester_is_minimal():
    perms = set(ROLE_PERMISSIONS["requester"])
    assert perms == {
        Permission.INTAKE_CREATE_TICKET,
        Permission.INTAKE_READ_OWN_TICKETS,
    }


def test_can_user_do_action_grant():
    perms = {Permission.INTAKE_CREATE_TICKET.value}
    assert can_user_do(perms, Permission.INTAKE_CREATE_TICKET).allowed
    assert not can_user_do(perms, Permission.INTAKE_APPROVE_RECOMMENDATION).allowed


def test_can_user_do_scope_layer():
    perms = {Permission.MATTER_READ_ASSIGNED.value}
    assert can_user_do(perms, Permission.MATTER_READ_ASSIGNED, scope_ok=True).allowed
    denied = can_user_do(perms, Permission.MATTER_READ_ASSIGNED, scope_ok=False)
    assert not denied.allowed
    assert denied.reason == "out_of_scope"
