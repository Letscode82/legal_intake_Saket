"""GraphRAG retrieval + Ask-the-Brain — traversal, permissions, gap notes.

The seed installs the demo ontology: Acme Corporation --NDA_WITH--> Org and
Document(NDA) --PARTY_TO--> Acme. These tests prove the doc's flagship
query — "do we have an NDA with Acme?" — resolves through the GRAPH (anchor
→ 1-hop → cited document), not just text luck, and that permission
filtering and gap notes hold.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.security import Actor
from app.db import graphrag
from app.db.models import Organization, Role, User
from app.db.ontology import NodeRef, add_edge
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _actor(session, email: str) -> Actor:
    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalars().first()
    role = await session.get(Role, user.role_id)
    return Actor(
        user_id=user.id,
        organization_id=user.organization_id,
        email=user.email,
        name=user.name,
        role_name=role.name,
        permissions=frozenset(role.permissions or []),
    )


async def test_nda_with_acme_resolves_through_the_graph(prepared_db):
    sm = get_sessionmaker()
    async with sm() as session:
        admin = await _actor(session, "alex.nguyen@aegis-demo.example")
        result = await graphrag.retrieve(
            session, admin, "do we have an NDA with Acme?"
        )
        assert result.retrieval_mode == "graph+fts"
        # Acme anchors the query…
        assert any(a.type == "Counterparty" and "Acme" in a.title
                   for a in result.anchors)
        # …and the executed NDA document is reached via the PARTY_TO edge.
        doc_hits = [h for h in result.hits if h.type == "Document"]
        assert doc_hits, [h.title for h in result.hits]
        assert any("NDA" in h.title and "PARTY_TO" in h.via_labels
                   for h in doc_hits)


async def test_two_hop_reach(prepared_db):
    # Org is 1 hop from Acme (NDA_WITH); the doc is 1 hop (PARTY_TO). Add a
    # 2-hop node: Person --WORKS_AT--> Acme, then anchor on the person.
    sm = get_sessionmaker()
    async with sm() as session:
        admin = await _actor(session, "alex.nguyen@aegis-demo.example")
        from app.db.models import Counterparty, Person

        acme = (
            await session.execute(
                select(Counterparty).where(Counterparty.name == "Acme Corporation")
            )
        ).scalars().first()
        contact = Person(
            organization_id=admin.organization_id,
            type="COUNTERPARTY_CONTACT",
            name="Jordan Vega",
            email="jordan.vega@acme.example",
        )
        session.add(contact)
        await session.flush()
        await add_edge(
            session, organization_id=admin.organization_id,
            src=NodeRef("Person", contact.id), label="WORKS_AT",
            dst=NodeRef("Counterparty", acme.id),
            source_module="test",
        )
        await session.commit()

        result = await graphrag.retrieve(
            session, admin, "what do we know about Jordan Vega?", k_hops=2
        )
        # 2 hops out from the person: Acme (1) and the NDA document (2).
        types_reached = {(h.type, h.hops) for h in result.hits}
        assert ("Person", 0) in {(h.type, h.hops) for h in result.anchors} or any(
            h.type == "Person" for h in result.hits
        )
        assert any(t == "Counterparty" for t, _ in types_reached)
        assert any(t == "Document" and hops == 2 for t, hops in types_reached)


async def test_permission_filtering_drops_unreadable_types(prepared_db):
    sm = get_sessionmaker()
    async with sm() as session:
        # external_counsel: matter reads only — no contracts:read_all, so
        # Document nodes must be invisible even though the graph reaches them.
        ext = await _actor(session, "external_counsel@aegis-demo.example")
        result = await graphrag.retrieve(session, ext, "NDA with Acme")
        assert all(h.type != "Document" for h in result.hits)
        assert all(a.type != "Document" for a in result.anchors)

        # requester: no reads at all → empty + explicit gap note.
        req = await _actor(session, "requester@aegis-demo.example")
        result = await graphrag.retrieve(session, req, "NDA with Acme")
        assert result.hits == []
        assert any("permission" in n for n in result.gap_notes)


async def test_gap_note_when_record_is_silent(prepared_db):
    sm = get_sessionmaker()
    async with sm() as session:
        admin = await _actor(session, "alex.nguyen@aegis-demo.example")
        result = await graphrag.retrieve(
            session, admin, "zorbulon flux capacitor litigation"
        )
        assert result.hits == []
        assert any("Absence of records" in n for n in result.gap_notes)


async def test_org_isolation(prepared_db):
    sm = get_sessionmaker()
    async with sm() as session:
        admin = await _actor(session, "alex.nguyen@aegis-demo.example")
        other = Organization(name="Other Tenant", tier="DEMO", region="US")
        session.add(other)
        await session.flush()
        from app.db.models import Counterparty

        session.add(
            Counterparty(
                organization_id=other.id, name="Acme Corporation", type="COMPANY"
            )
        )
        await session.commit()

        result = await graphrag.retrieve(session, admin, "NDA with Acme")
        assert all(
            h.id != other.id for h in result.hits
        )  # nothing from the other tenant
        # All counterparty hits belong to the admin's org (id equality is
        # checked implicitly — the other tenant's Acme row would double the
        # counterparty hits; assert exactly one Acme counterparty).
        acme_hits = [
            h for h in result.hits
            if h.type == "Counterparty" and "Acme" in h.title
        ]
        assert len(acme_hits) == 1


async def test_brain_endpoint_degraded_answer_with_citations(client):
    r = await client.post(
        "/api/v1/brain/query", json={"question": "do we have an NDA with Acme?"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ai_synthesis"] is False  # no ANTHROPIC_API_KEY in tests
    assert body["retrieval_mode"] == "graph+fts"
    assert any(c["type"] == "Document" for c in body["citations"])
    assert "Acme" in body["answer"]


async def test_brain_endpoint_permission_filtered_for_requester(client):
    r = await client.post(
        "/api/v1/brain/query",
        json={"question": "do we have an NDA with Acme?"},
        headers={"X-Dev-User-Email": "requester@aegis-demo.example"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["citations"] == []
    assert body["gap_notes"]
