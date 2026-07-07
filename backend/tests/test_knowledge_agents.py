"""FAQ + Policy Q&A agents (Knowledge module, PRs 12+13).

Uses a LOCAL FastAPI app (main.py wiring for the knowledge router lands
separately) mounting only the knowledge router, exercised through the same
Postgres test database the session-scoped ``prepared_db`` fixture builds —
the same pattern as tests/test_cockpit_decisions.py.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

from app.db.models import Event, KnowledgeEntry, OntologyEdge, Organization
from app.db.session import get_sessionmaker
from app.modules.knowledge.router import router as knowledge_router

pytestmark = pytest.mark.asyncio(loop_scope="session")

test_app = FastAPI()
test_app.include_router(knowledge_router, prefix="/api/v1")


@pytest.fixture(autouse=True)
def _no_ai(monkeypatch):
    """Force degraded mode so answers quote the verbatim approved body —
    deterministic assertions regardless of any ambient ANTHROPIC_API_KEY."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "anthropic_api_key", None)


@pytest_asyncio.fixture(loop_scope="session")
async def knowledge_client(prepared_db):
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _demo_org_id(session) -> str:
    org = (
        await session.execute(
            select(Organization).where(Organization.name == "AEGIS Demo GC")
        )
    ).scalars().first()
    assert org is not None
    return org.id


async def test_faq_hit_quotes_approved_body_with_citation(knowledge_client):
    resp = await knowledge_client.post(
        "/api/v1/knowledge/ask", json={"question": "how long does an NDA take?"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["answered"] is True
    assert body["conflict"] is False
    assert body["handoff"] is None
    assert "1 business day" in body["answer"]
    assert "general guidance from the approved knowledge base" in body["answer"]
    assert "not legal advice" in body["answer"]

    assert len(body["citations"]) == 1
    citation = body["citations"][0]
    assert citation["type"] == "KnowledgeEntry"
    assert citation["slug"] == "nda-turnaround"
    assert citation["version"] == 1

    sm = get_sessionmaker()
    async with sm() as session:
        entry = (
            await session.execute(
                select(KnowledgeEntry).where(
                    KnowledgeEntry.slug == "nda-turnaround",
                    KnowledgeEntry.is_current.is_(True),
                )
            )
        ).scalars().first()
        assert citation["id"] == entry.id


async def test_faq_miss_hands_off_to_front_door(knowledge_client):
    resp = await knowledge_client.post(
        "/api/v1/knowledge/ask",
        json={"question": "what is the wifi password for the guest network?"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["answered"] is False
    assert body["answer"] is None
    assert body["citations"] == []
    assert body["handoff"] is not None
    assert "front-door" in body["handoff"]["hint"]
    assert body["handoff"]["suggested_request_type"] == "contract"


async def test_faq_escalation_regex_always_hands_off(knowledge_client):
    # "deadline" + "lawsuit" trip the deterministic escalation rule even
    # though tokens ("respond", "lawsuit", ...) could FTS-match entries.
    resp = await knowledge_client.post(
        "/api/v1/knowledge/ask",
        json={"question": "what is the deadline to respond to this lawsuit?"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["answered"] is False
    assert body["handoff"] is not None
    assert body["handoff"]["reason"] == "routed to the legal queue"
    assert body["citations"] == []


async def test_policy_quote_is_verbatim_with_versioned_citation(knowledge_client):
    resp = await knowledge_client.post(
        "/api/v1/knowledge/policy-ask",
        json={"question": "can I accept a gift from a vendor?"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["answered"] is True
    assert body["conflict"] is False
    assert "$150" in body["answer"]  # verbatim from the approved body

    assert len(body["citations"]) == 1
    citation = body["citations"][0]
    assert citation["title"] == "Gifts & Entertainment Policy"
    assert citation["version"] == 1
    assert "effective_date" in citation


async def test_policy_conflict_routes_to_owner_not_harmonized(knowledge_client):
    sm = get_sessionmaker()
    async with sm() as session:
        org_id = await _demo_org_id(session)
        conflicting = KnowledgeEntry(
            organization_id=org_id,
            kind="POLICY",
            slug="gifts-regional-apac",
            topic="gifts",
            title="Gifts Policy (APAC Regional)",
            body=(
                "Employees may accept gifts up to $500 in value from any "
                "vendor. Gifts must be logged in the regional gift register."
            ),
            version=1,
            is_current=True,
        )
        session.add(conflicting)
        await session.commit()
        conflicting_id = conflicting.id

    try:
        resp = await knowledge_client.post(
            "/api/v1/knowledge/policy-ask",
            json={"question": "can I accept a gift from a vendor?"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["conflict"] is True
        assert body["answered"] is False
        assert "routed to the policy owner" in body["answer"]
        assert len(body["citations"]) == 2
        cited_ids = {c["id"] for c in body["citations"]}
        assert conflicting_id in cited_ids

        async with sm() as session:
            conflict_events = (
                await session.execute(
                    select(Event).where(Event.type == "knowledge.policy_conflict")
                )
            ).scalars().all()
            assert len(conflict_events) >= 1
    finally:
        # Retire the inserted row so other tests see one current gifts policy.
        async with sm() as session:
            row = await session.get(KnowledgeEntry, conflicting_id)
            row.is_current = False
            await session.commit()


async def test_answer_record_event_and_cites_edge_written(knowledge_client):
    resp = await knowledge_client.post(
        "/api/v1/knowledge/ask", json={"question": "how long does an NDA take?"}
    )
    assert resp.status_code == 200
    assert resp.json()["answered"] is True

    sm = get_sessionmaker()
    async with sm() as session:
        events = (
            await session.execute(
                select(Event).where(Event.type == "knowledge.answer")
            )
        ).scalars().all()
        assert len(events) >= 1

        answer_events = [
            e for e in events
            if e.source_type == "KnowledgeEntry"
            and e.payload.get("kind") == "FAQ"
            and e.actor_id is not None
        ]
        assert answer_events

        event = answer_events[-1]
        edges = (
            await session.execute(
                select(OntologyEdge).where(
                    OntologyEdge.src_type == "Event",
                    OntologyEdge.src_id == event.id,
                    OntologyEdge.label == "CITES",
                )
            )
        ).scalars().all()
        assert len(edges) >= 1
        assert edges[0].dst_type == "KnowledgeEntry"
        assert edges[0].dst_id == event.source_id
        assert edges[0].properties.get("version") == 1
        assert edges[0].source_module == "knowledge"
