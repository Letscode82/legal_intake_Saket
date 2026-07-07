"""Audit chain tamper-detection.

Even if an attacker with superuser disables the immutability triggers and
edits a sealed row, ``verify_audit_chain`` must catch it — the verifier
recomputes each row's hash from its stored fields and does not depend on the
triggers being intact.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.audit import log_audit, verify_audit_chain
from app.db.models import Organization
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_tamper_is_detected_even_with_triggers_disabled(prepared_db):
    sm = get_sessionmaker()
    async with sm() as session:
        # Isolated org — this test deliberately corrupts its own chain, so it
        # must not touch the demo org other tests verify.
        org = Organization(name="Tamper Test Org", tier="DEMO", region="US")
        session.add(org)
        await session.flush()
        org_id = org.id
        for i in range(4):
            await log_audit(
                session,
                organization_id=org_id,
                actor_type="USER",
                actor_id="u1",
                action="test.tamper",
                resource_type="Test",
                resource_id=f"row{i}",
                after_json={"seq": i},
            )
        await session.commit()

        # Baseline: chain is intact.
        assert (await verify_audit_chain(session, org_id)).ok

        # Attacker disables triggers (superuser) and edits a sealed row's
        # payload WITHOUT recomputing the hash.
        await session.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update"))
        await session.execute(
            text(
                "UPDATE audit_log SET after_json = '{\"seq\": 999}' "
                "WHERE organization_id = :org AND resource_id = 'row2'"
            ),
            {"org": org_id},
        )
        await session.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_update"))
        await session.commit()

        result = await verify_audit_chain(session, org_id)
        assert result.ok is False
        assert result.reason is not None
        assert result.broken_at_position is not None
