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

    name = "tei"

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


_LOCAL_MODEL_CACHE: dict[str, object] = {}


class LocalEmbeddingProvider:
    """In-process embeddings — NO server to host.

    Runs the model inside the FastAPI process via ``fastembed`` (ONNX, no
    torch — light) with a ``sentence-transformers`` fallback. The model is
    downloaded once to a local cache on first use, then served from memory.
    Default is ``BAAI/bge-m3`` (1024-dim, 8192 ctx, Apache-2.0). On a
    memory-constrained box set ``EMBEDDINGS_MODEL=BAAI/bge-small-en-v1.5``
    (~130 MB, 384-dim) — the stored vector length adapts automatically.

    Degrades to None (FTS-only) if neither library is installed or the model
    cannot be fetched — so an air-gapped or download-blocked environment
    never crashes; it just runs without the vector leg.

    Install the extra: ``pip install '.[local-embeddings]'``.
    """

    name = "local"

    def __init__(self) -> None:
        self._model_name = settings.embeddings_model
        self.dim = settings.embeddings_dim

    def _load(self):
        if self._model_name in _LOCAL_MODEL_CACHE:
            return _LOCAL_MODEL_CACHE[self._model_name]
        model = None
        try:
            from fastembed import TextEmbedding

            model = ("fastembed", TextEmbedding(model_name=self._model_name))
        except Exception:  # noqa: BLE001 — try the heavier fallback
            try:
                from sentence_transformers import SentenceTransformer

                model = ("st", SentenceTransformer(self._model_name))
            except Exception:  # noqa: BLE001 — no local backend available
                logger.warning(
                    "local embedding backend unavailable (install "
                    "'.[local-embeddings]' and ensure the model can be "
                    "fetched); degrading to FTS-only",
                    exc_info=True,
                )
                model = None
        _LOCAL_MODEL_CACHE[self._model_name] = model
        return model

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not texts:
            return None
        model = self._load()
        if model is None:
            return None
        kind, impl = model
        try:
            # Model inference is CPU-bound; run it off the event loop.
            import anyio

            def _run() -> list[list[float]]:
                if kind == "fastembed":
                    return [list(map(float, v)) for v in impl.embed(texts)]
                return [list(map(float, v)) for v in impl.encode(texts)]

            return await anyio.to_thread.run_sync(_run)
        except Exception:  # noqa: BLE001 — degrade on any inference failure
            logger.warning("local embed failed; degrading to FTS-only", exc_info=True)
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
    if provider in {"local", "bge-m3"}:
        # In-process BGE-M3; no URL/key needed. Runtime-degrades if the model
        # can't load, so it's safe to select unconditionally.
        return LocalEmbeddingProvider()
    if provider in {"tei", "http"} and settings.embeddings_url:
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
