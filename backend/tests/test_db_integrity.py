"""DB-integrity: migrations-from-scratch + seed + chain verifies.

This is the required pre-merge gate analogue: the ``prepared_db`` fixture
applies every migration on an empty database, seeds it, and here we assert
the seeded org's audit chain verifies and the immutability triggers are live.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from app.core.audit import log_audit, verify_audit_chain
from app.db.models import Organization
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _demo_org_id(session) -> str:
    org = (
        await session.execute(
            select(Organization).where(Organization.name == "AEGIS Demo GC")
        )
    ).scalars().first()
    assert org is not None
    return org.id


async def test_seeded_chain_verifies(prepared_db):
    sm = get_sessionmaker()
    async with sm() as session:
        org_id = await _demo_org_id(session)
        # Write a few rows through the real path.
        for i in range(3):
            await log_audit(
                session,
                organization_id=org_id,
                actor_type="SYSTEM",
                action="test.event",
                resource_type="Test",
                resource_id=f"t{i}",
                after_json={"i": i},
            )
        await session.commit()
        result = await verify_audit_chain(session, org_id)
        assert result.ok, result.problems
        assert result.rows_checked >= 3


async def test_update_and_delete_are_blocked(prepared_db):
    sm = get_sessionmaker()
    async with sm() as session:
        org_id = await _demo_org_id(session)
        await log_audit(
            session,
            organization_id=org_id,
            actor_type="SYSTEM",
            action="test.immutable",
            resource_type="Test",
            resource_id="immutable",
        )
        await session.commit()

    async with sm() as session:
        with pytest.raises(Exception) as exc:
            await session.execute(
                text("UPDATE audit_log SET action = 'x' WHERE action = 'test.immutable'")
            )
            await session.commit()
        assert "append-only" in str(exc.value).lower() or "forbidden" in str(exc.value).lower()

    async with sm() as session:
        with pytest.raises(Exception):
            await session.execute(
                text("DELETE FROM audit_log WHERE action = 'test.immutable'")
            )
            await session.commit()
