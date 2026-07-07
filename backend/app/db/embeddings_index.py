"""Ontology embedding indexer.

Builds a compact text representation of each shared-entity node, embeds it
through the configured provider, and upserts the vector into
``ontology_embedding``. Content-hashed so unchanged nodes are skipped on
re-index. Lives in the db package (the sanctioned home for the retrieval
SQL). Degrades cleanly: with no provider, ``reindex_org`` is a no-op that
reports zero indexed — GraphRAG simply stays FTS-only.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embeddings import EmbeddingProvider, get_embedding_provider
from app.db.models import (
    Counterparty,
    Document,
    IntakeTicket,
    OntologyEmbedding,
    Person,
)

# Node types that are embeddable, each with a text builder. Mirrors the
# citable types in graphrag so vectors and citations stay aligned.
_BUILDERS = {
    "Counterparty": lambda r: f"{r.name} ({r.type}{(', ' + r.country) if r.country else ''})",
    "Person": lambda r: f"{r.name} {r.email or ''} ({r.type})".strip(),
    "Document": lambda r: f"{r.name}\n{(r.extracted_text or '')[:2000]}".strip(),
    "IntakeTicket": lambda r: f"{r.type}\n{r.description}",
}
_MODELS = {
    "Counterparty": Counterparty,
    "Person": Person,
    "Document": Document,
    "IntakeTicket": IntakeTicket,
}


@dataclass
class ReindexResult:
    organization_id: str
    provider: str
    embedded: int
    skipped: int
    scanned: int


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def reindex_org(
    session: AsyncSession,
    organization_id: str,
    *,
    provider: EmbeddingProvider | None = None,
) -> ReindexResult:
    """(Re)embed every embeddable node in an org. Idempotent via content hash."""
    provider = provider or get_embedding_provider()
    scanned = embedded = skipped = 0

    if provider.name == "none":
        return ReindexResult(organization_id, "none", 0, 0, 0)

    for node_type, model in _MODELS.items():
        rows = (
            await session.execute(
                select(model).where(model.organization_id == organization_id)
            )
        ).scalars().all()
        build = _BUILDERS[node_type]

        pending: list[tuple[str, str, str]] = []  # (node_id, text, hash)
        existing = {
            (e.node_id): e
            for e in (
                await session.execute(
                    select(OntologyEmbedding).where(
                        OntologyEmbedding.organization_id == organization_id,
                        OntologyEmbedding.node_type == node_type,
                    )
                )
            ).scalars()
        }
        for r in rows:
            scanned += 1
            text = build(r)
            h = _content_hash(text)
            prior = existing.get(r.id)
            if prior is not None and prior.content_hash == h and prior.model == provider.name:
                skipped += 1
                continue
            pending.append((r.id, text, h))

        if not pending:
            continue

        vectors = await provider.embed([t for _, t, _ in pending])
        if vectors is None:
            # Provider failed mid-run — leave what we have, report degrade.
            return ReindexResult(
                organization_id, provider.name, embedded, skipped, scanned
            )

        for (node_id, _text, h), vec in zip(pending, vectors):
            prior = existing.get(node_id)
            if prior is not None:
                prior.embedding = vec
                prior.content_hash = h
                prior.model = provider.name
                prior.dim = len(vec)
            else:
                session.add(
                    OntologyEmbedding(
                        organization_id=organization_id,
                        node_type=node_type,
                        node_id=node_id,
                        model=provider.name,
                        dim=len(vec),
                        embedding=vec,
                        content_hash=h,
                    )
                )
            embedded += 1

    await session.commit()
    return ReindexResult(organization_id, provider.name, embedded, skipped, scanned)


async def load_embeddings(
    session: AsyncSession,
    organization_id: str,
    keys: list[tuple[str, str]],
) -> dict[tuple[str, str], list[float]]:
    """Fetch stored vectors for a set of (node_type, node_id) candidates."""
    if not keys:
        return {}
    types = {k[0] for k in keys}
    ids = {k[1] for k in keys}
    rows = (
        await session.execute(
            select(OntologyEmbedding).where(
                OntologyEmbedding.organization_id == organization_id,
                OntologyEmbedding.node_type.in_(types),
                OntologyEmbedding.node_id.in_(ids),
            )
        )
    ).scalars().all()
    wanted = set(keys)
    return {
        (r.node_type, r.node_id): list(r.embedding)
        for r in rows
        if (r.node_type, r.node_id) in wanted
    }
