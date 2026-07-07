"""Embedding provider abstraction — the GraphRAG vector leg.

Claude does not produce embeddings, so the vector side of hybrid retrieval
needs its own provider. Voyage AI is the Anthropic-recommended pairing and
``voyage-law-2`` is legal-domain-tuned. When no key is configured (or the
call fails) the provider is Null and retrieval runs FTS-only — the same
degrade discipline as the AI client: absent capability narrows quality,
never breaks the pipeline.

Vector STORAGE (pgvector column + event-driven indexer) activates in PR 20
on environments where the ``vector`` extension exists (Neon has it native).
This interface is the stable seam; no caller changes when storage lands.
"""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

from app.core.config import settings

logger = logging.getLogger("aegis.embeddings")

_VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"


class EmbeddingProvider(Protocol):
    name: str

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Vectors for ``texts``, or None when unavailable (degrade)."""
        ...


class NullEmbeddingProvider:
    """No provider configured — retrieval runs FTS-only."""

    name = "none"

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        return None


class VoyageEmbeddingProvider:
    name = "voyage"

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    _VOYAGE_URL,
                    headers={"Authorization": f"Bearer {settings.voyage_api_key}"},
                    json={"model": settings.voyage_model, "input": texts},
                )
                resp.raise_for_status()
                data = resp.json()["data"]
                return [row["embedding"] for row in data]
        except Exception:  # noqa: BLE001 — degrade, never break retrieval
            logger.warning("voyage embed failed; degrading to FTS-only", exc_info=True)
            return None


def get_embedding_provider() -> EmbeddingProvider:
    if settings.voyage_api_key:
        return VoyageEmbeddingProvider()
    return NullEmbeddingProvider()
