"""Defensibility export — off-database re-verification.

The export's whole purpose is that an auditor WITHOUT database access can
prove the ledger intact: SHA-256 each row's verbatim canonical_content and
compare to content_hash, then walk prev_hash linkage. This test performs
exactly that, in Python, against the live export payload.
"""

from __future__ import annotations

import hashlib

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_export_reverifies_off_database(client):
    # Ensure at least one governed mutation is on the ledger.
    r = await client.post(
        "/api/v1/intake/tickets",
        json={
            "description": "Please prepare an NDA with Vertex Labs.",
            "requester_name": "Dana Li",
            "requester_email": "dana.li@aegis-demo.example",
        },
    )
    assert r.status_code == 201

    r = await client.get("/api/v1/audit/export")
    assert r.status_code == 200
    export = r.json()

    assert export["schema_id"] == "aegis.audit.defensibility.v1"
    assert export["verification"]["ok"] is True
    assert export["row_count"] == len(export["rows"]) > 0

    # The auditor's computation: hash the verbatim canonical content.
    prev = "0" * 64
    for i, row in enumerate(export["rows"], start=1):
        assert row["chain_position"] == i, "positions must be contiguous"
        assert row["prev_hash"] == prev, "each row must link to its predecessor"
        digest = hashlib.sha256(row["canonical_content"].encode("utf-8")).hexdigest()
        assert digest == row["content_hash"], (
            f"row {i}: stored hash does not match recomputed hash"
        )
        prev = row["content_hash"]


async def test_export_requires_audit_read_permission(client):
    r = await client.get(
        "/api/v1/audit/export",
        headers={"X-Dev-User-Email": "requester@aegis-demo.example"},
    )
    assert r.status_code == 403
