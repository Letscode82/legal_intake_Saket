"""Embedding provider abstraction — the GraphRAG vector leg.

Claude does not produce embeddings, so the vector side of hybrid retrieval
needs its own provider. The DEFAULT is a **self-hosted BAAI/BGE-M3** model
served over HTTP (HuggingFace Text-Embeddings-Inference), chosen for a legal
platform because privileged content never leaves our region/infra, the
licence is Apache-2.0, the context window (8192) fits contracts/notices,
and its dense+sparse design complements our graph+FTS fusion. A hosted
Voyage provider stays available as an alternative.

Degrade discipline (identical to core/ai.py): when no provider is
configured, or the endpoint is unreachable, or a call fails, ``embed``
returns None and retrieval runs FTS-only. Absent capability narrows
quality; it never breaks the pipeline.

The provider is model-agnostic at the HTTP seam: any TEI / OpenAI-compatible
endpoint (BGE-M3, E5, a future Microsoft OSS model, …) works by pointing
``EMBEDDINGS_URL`` / ``EMBEDDINGS_MODEL`` at it — no code change.
"""

from __future__ import annotations

import logging
import math
from typing import Protocol

import httpx

from app.core.config import settings

logger = logging.getLogger("aegis.embeddings")


class EmbeddingProvider(Protocol):
    name: str
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Vectors for ``texts``, or None when unavailable (degrade)."""
        ...


class NullEmbeddingProvider:
    """No provider configured — retrieval runs FTS-only."""

    name = "none"
    dim = 0

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        return None


class HTTPEmbeddingProvider:
    """Self-hosted embeddings over HTTP (default: BAAI/bge-m3 via HF TEI).

    Speaks the two common shapes and auto-detects the response:
      * TEI:            POST {"inputs": [...]}          -> [[...], ...]
      * OpenAI-compat:  POST {"model": m, "input": [...]} -> {"data":[{"embedding":[...]}]}
    """

    name = "bge-m3"

    def __init__(self) -> None:
        self.dim = settings.embeddings_dim
        self._url = (settings.embeddings_url or "").rstrip("/")
        self._model = settings.embeddings_model
        self._key = settings.embeddings_api_key

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not self._url or not texts:
            return None
        headers = {"content-type": "application/json"}
        if self._key:
            headers["authorization"] = f"Bearer {self._key}"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # Prefer the OpenAI-compatible route if the URL looks like one,
                # else TEI. Both are attempted defensively.
                if self._url.endswith("/embeddings") or self._url.endswith("/v1"):
                    resp = await client.post(
                        self._url if self._url.endswith("/embeddings")
                        else f"{self._url}/embeddings",
                        headers=headers,
                        json={"model": self._model, "input": texts},
                    )
                    resp.raise_for_status()
                    return [row["embedding"] for row in resp.json()["data"]]
                resp = await client.post(
                    f"{self._url}/embed" if not self._url.endswith("/embed") else self._url,
                    headers=headers,
                    json={"inputs": texts},
                )
                resp.raise_for_status()
                data = resp.json()
                # TEI returns a bare list-of-lists.
                return data if isinstance(data, list) else data.get("embeddings")
        except Exception:  # noqa: BLE001 — degrade, never break retrieval
            logger.warning(
                "embedding endpoint failed; degrading to FTS-only", exc_info=True
            )
            return None


class VoyageEmbeddingProvider:
    name = "voyage"

    def __init__(self) -> None:
        self.dim = settings.embeddings_dim

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not settings.voyage_api_key or not texts:
            return None
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    "https://api.voyageai.com/v1/embeddings",
                    headers={"Authorization": f"Bearer {settings.voyage_api_key}"},
                    json={"model": settings.voyage_model, "input": texts},
                )
                resp.raise_for_status()
                return [row["embedding"] for row in resp.json()["data"]]
        except Exception:  # noqa: BLE001
            logger.warning("voyage embed failed; degrading to FTS-only", exc_info=True)
            return None


def get_embedding_provider() -> EmbeddingProvider:
    provider = settings.embeddings_provider.lower()
    if provider in {"bge-m3", "http"} and settings.embeddings_url:
        return HTTPEmbeddingProvider()
    if provider == "voyage" and settings.voyage_api_key:
        return VoyageEmbeddingProvider()
    return NullEmbeddingProvider()


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity in [-1, 1]; 0.0 on degenerate input."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
