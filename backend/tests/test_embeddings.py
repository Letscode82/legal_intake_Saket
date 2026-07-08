"""Embeddings vector leg — provider degrade, indexer, and hybrid fusion.

The real BGE-M3 model can't download in CI, so these use a DETERMINISTIC
fake embedder (a bag-of-words hashed vector) to prove the full plumbing:
index → store (portable float8[]) → cosine rerank inside GraphRAG. Semantic
quality is the model's job; correctness of the pipeline is ours, and that is
what is verified here.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select

from app.core.embeddings import NullEmbeddingProvider, cosine, get_embedding_provider
from app.core.security import Actor
from app.db import graphrag
from app.db.embeddings_index import reindex_org
from app.db.models import OntologyEmbedding, Role, User
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.asyncio(loop_scope="session")

_DIM = 64


class FakeEmbedder:
    """Deterministic bag-of-words hashed embedding — same words → similar
    vectors, so cosine ordering is meaningful and reproducible."""

    name = "fake"
    dim = _DIM

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * _DIM
            for tok in text.lower().split():
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
                vec[h % _DIM] += 1.0
            out.append(vec)
        return out


async def _admin(session) -> Actor:
    u = (
        await session.execute(
            select(User).where(User.email == "alex.nguyen@aegis-demo.example")
        )
    ).scalars().first()
    r = await session.get(Role, u.role_id)
    return Actor(
        user_id=u.id, organization_id=u.organization_id, email=u.email,
        name=u.name, role_name=r.name, permissions=frozenset(r.permissions or []),
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_cosine_and_default_provider():
    from app.core.embeddings import LocalEmbeddingProvider

    assert cosine([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine([], [1]) == 0.0  # degenerate → safe
    # Default is in-process BGE-M3 (no server to host). It runtime-degrades
    # if the model can't be fetched — never crashes.
    assert isinstance(get_embedding_provider(), LocalEmbeddingProvider)


async def test_reindex_noop_with_null_provider(prepared_db):
    sm = get_sessionmaker()
    async with sm() as session:
        actor = await _admin(session)
        # Explicit Null provider → clean no-op (FTS-only), regardless of the
        # configured default.
        result = await reindex_org(
            session, actor.organization_id, provider=NullEmbeddingProvider()
        )
        assert result.provider == "none"
        assert result.embedded == 0


async def test_reindex_embeds_and_is_idempotent(prepared_db):
    sm = get_sessionmaker()
    fake = FakeEmbedder()
    async with sm() as session:
        actor = await _admin(session)
        r1 = await reindex_org(session, actor.organization_id, provider=fake)
        assert r1.embedded > 0 and r1.provider == "fake"
        first = r1.embedded

    async with sm() as session:
        actor = await _admin(session)
        # Nothing changed → all skipped (content-hash short-circuit).
        r2 = await reindex_org(session, actor.organization_id, provider=fake)
        assert r2.embedded == 0
        assert r2.skipped >= first

    async with sm() as session:
        rows = (
            await session.execute(
                select(OntologyEmbedding).where(
                    OntologyEmbedding.node_type == "Counterparty"
                )
            )
        ).scalars().all()
        assert rows and all(len(r.embedding) == _DIM for r in rows)


async def test_vector_rerank_changes_mode_and_ordering(prepared_db):
    sm = get_sessionmaker()
    fake = FakeEmbedder()
    async with sm() as session:
        actor = await _admin(session)
        await reindex_org(session, actor.organization_id, provider=fake)

    async with sm() as session:
        actor = await _admin(session)
        # With the fake embedder the mode advertises the vector leg…
        result = await graphrag.retrieve(
            session, actor, "NDA with Acme Corporation", embedder=fake
        )
        assert "vector" in result.retrieval_mode
        assert result.hits
        # …and scores are non-increasing (rerank re-sorted correctly).
        scores = [h.score for h in result.hits]
        assert scores == sorted(scores, reverse=True)

    async with sm() as session:
        actor = await _admin(session)
        # Null embedder → graph+fts only, still works.
        result = await graphrag.retrieve(
            session, actor, "NDA with Acme Corporation",
            embedder=NullEmbeddingProvider(),
        )
        assert result.retrieval_mode == "graph+fts"


async def test_local_provider_selection_and_degrade(monkeypatch):
    """EMBEDDINGS_PROVIDER=local runs in-process (no server). Here the model
    download is blocked, so embed() must DEGRADE to None rather than crash —
    the same behavior an air-gapped deploy sees."""
    from app.core import config as cfg
    from app.core import embeddings as emb

    monkeypatch.setattr(cfg.settings, "embeddings_provider", "local")
    provider = emb.get_embedding_provider()
    assert isinstance(provider, emb.LocalEmbeddingProvider)
    # No crash — returns vectors where the model is reachable, None where it
    # isn't (this sandbox: HF is proxy-denied → None).
    out = await provider.embed(["NDA with Acme"])
    assert out is None or (isinstance(out, list) and isinstance(out[0], list))


async def test_admin_reindex_endpoint_gated(client):
    r = await client.post(
        "/api/v1/admin/jobs/reindex-embeddings",
        headers={"X-Dev-User-Email": "requester@aegis-demo.example"},
    )
    assert r.status_code == 403
    r = await client.post("/api/v1/admin/jobs/reindex-embeddings")
    assert r.status_code == 200
    body = r.json()
    assert "embedded" in body and "provider" in body
    # Default provider is in-process "local"; the model can't download in
    # this sandbox, so it degrades to 0 embedded without crashing.
    assert body["embedded"] == 0
