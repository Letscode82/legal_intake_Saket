"""Audit ledger write path + chain verification.

Every state-changing operation writes an ``audit_log`` row through
``log_audit`` inside the SAME transaction as the mutation. Unlike the
frontend's best-effort helper, this raises on failure: if the audit row
cannot be written, the mutation must not commit. The audit row IS the legal
anchor.

The Postgres BEFORE INSERT trigger (migration 0002) fills ``prev_hash`` /
``content_hash`` / ``chain_position``. ``verify_audit_chain`` recomputes
each row's hash off the stored fields using the SAME SQL helper the trigger
used (``audit_log_compute_hash``) — so tamper detection does NOT depend on
the immutability triggers still being installed. An attacker with superuser
could drop the triggers, but any edit to a sealed row's fields makes its
recomputed hash diverge, and the mismatch localises the break.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import gen_id

_GENESIS_PREV_HASH = "0" * 64


async def log_audit(
    session: AsyncSession,
    *,
    organization_id: str,
    action: str,
    resource_type: str,
    resource_id: str,
    actor_id: str | None = None,
    actor_type: str = "USER",
    before_json: dict | None = None,
    after_json: dict | None = None,
    metadata: dict | None = None,
) -> str:
    """Insert one audit row. Returns the new row id.

    Does NOT commit — the caller's service commits the mutation and this row
    together. Raises if the insert fails so the surrounding transaction
    rolls back (no mutation without an audit row).
    """
    audit_id = gen_id()
    await session.execute(
        text(
            """
            INSERT INTO audit_log (
                id, organization_id, actor_id, actor_type, action,
                resource_type, resource_id, before_json, after_json,
                metadata, schema_version
            ) VALUES (
                :id, :organization_id, :actor_id, :actor_type, :action,
                :resource_type, :resource_id,
                CAST(:before_json AS jsonb), CAST(:after_json AS jsonb),
                CAST(:metadata AS jsonb), 1
            )
            """
        ),
        {
            "id": audit_id,
            "organization_id": organization_id,
            "actor_id": actor_id,
            "actor_type": actor_type,
            "action": action,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "before_json": _json_or_none(before_json),
            "after_json": _json_or_none(after_json),
            "metadata": _json_or_none(metadata),
        },
    )
    return audit_id


def _json_or_none(value: dict | None) -> str | None:
    import json

    return None if value is None else json.dumps(value)


@dataclass
class ChainVerificationResult:
    ok: bool
    rows_checked: int
    broken_at_position: int | None = None
    reason: str | None = None
    problems: list[str] = field(default_factory=list)


async def verify_audit_chain(
    session: AsyncSession, organization_id: str
) -> ChainVerificationResult:
    """Walk an organization's chain forward and prove it is intact."""
    rows = (
        await session.execute(
            text(
                """
                SELECT id, actor_id, actor_type, action, resource_type,
                       resource_id, before_json, after_json, metadata,
                       timestamp, prev_hash, content_hash, chain_position,
                       schema_version
                FROM audit_log
                WHERE organization_id = :org
                ORDER BY chain_position ASC
                """
            ),
            {"org": organization_id},
        )
    ).mappings().all()

    problems: list[str] = []
    expected_prev = _GENESIS_PREV_HASH
    expected_pos = 1

    for row in rows:
        pos = int(row["chain_position"])
        if pos != expected_pos:
            problems.append(
                f"position gap: expected {expected_pos}, found {pos} (id={row['id']})"
            )
            return ChainVerificationResult(
                ok=False,
                rows_checked=expected_pos - 1,
                broken_at_position=pos,
                reason="chain_position not contiguous",
                problems=problems,
            )

        if row["prev_hash"] != expected_prev:
            problems.append(
                f"prev_hash mismatch at position {pos} (id={row['id']})"
            )
            return ChainVerificationResult(
                ok=False,
                rows_checked=pos - 1,
                broken_at_position=pos,
                reason="prev_hash does not link to prior row",
                problems=problems,
            )

        # Recompute content_hash from stored fields via the SQL helper.
        recomputed = (
            await session.execute(
                text(
                    """
                    SELECT audit_log_compute_hash(
                        :schema_version, :org, :actor_id, :actor_type,
                        :action, :resource_type, :resource_id,
                        CAST(:before_json AS jsonb),
                        CAST(:after_json AS jsonb),
                        CAST(:metadata AS jsonb),
                        CAST(:timestamp AS timestamp),
                        :prev_hash, :chain_position
                    ) AS h
                    """
                ),
                {
                    "schema_version": int(row["schema_version"]),
                    "org": organization_id,
                    "actor_id": row["actor_id"],
                    "actor_type": row["actor_type"],
                    "action": row["action"],
                    "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"],
                    "before_json": _dumps(row["before_json"]),
                    "after_json": _dumps(row["after_json"]),
                    "metadata": _dumps(row["metadata"]),
                    "timestamp": row["timestamp"],
                    "prev_hash": row["prev_hash"],
                    "chain_position": pos,
                },
            )
        ).scalar_one()

        if recomputed != row["content_hash"]:
            problems.append(
                f"content_hash mismatch at position {pos} (id={row['id']}): "
                "row fields were altered after sealing"
            )
            return ChainVerificationResult(
                ok=False,
                rows_checked=pos - 1,
                broken_at_position=pos,
                reason="content_hash does not match stored fields (tampering)",
                problems=problems,
            )

        expected_prev = row["content_hash"]
        expected_pos += 1

    return ChainVerificationResult(ok=True, rows_checked=len(rows))


def _dumps(value: Any) -> str | None:
    """Serialize a JSONB column value back to text for the SQL hash helper.

    asyncpg returns JSONB as a Python object (dict/list) or as a str. The
    trigger hashed ``<col>::text`` (Postgres JSONB normalized text). Passing
    the value bound as ``jsonb`` and letting the helper cast it back to text
    reproduces that exact normalization.
    """
    import json

    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value)
