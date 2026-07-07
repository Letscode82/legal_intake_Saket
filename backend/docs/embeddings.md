# Embeddings — the GraphRAG vector leg

AEGIS completes the "brain" with a **self-hosted BAAI/BGE-M3** embedding
model. For a Fortune-50 legal platform this is deliberate: privileged
content is embedded in-region on our own infra, never sent to a third-party
API. BGE-M3 is Apache-2.0 (commercially safe), has an 8192-token context
(fits contracts and notices), and its dense+sparse design complements our
graph + full-text fusion. Jina v3 was rejected (CC-BY-NC — non-commercial);
Voyage remains available as a hosted alternative.

## How it fits the pipeline

```
query ──embed──▶ query vector
records ──index──▶ ontology_embedding (float8[])   (admin reindex job)
retrieve: graph k-hop → candidates → FTS+hop score
                                    → + cosine(query, node) rerank   ← vector leg
```

- **Portable storage.** Vectors live in `ontology_embedding.embedding` as a
  Postgres `float8[]`, so the schema applies everywhere (including sandboxes
  without the `vector` extension). Cosine reranking runs in Python over the
  graph-narrowed candidate set — already small — so **no pgvector is needed
  for correctness**.
- **Degrade discipline.** No `EMBEDDINGS_URL` → `NullEmbeddingProvider` →
  retrieval runs FTS-only. A model swap or endpoint outage never breaks the
  pipeline; it only narrows quality. `retrieval_mode` reports what ran
  (`graph+fts+vector`, `graph+fts`, `fts+vector`, `fts-only`).
- **Model-agnostic seam.** Any TEI / OpenAI-compatible endpoint works by
  pointing `EMBEDDINGS_URL` / `EMBEDDINGS_MODEL` at it — BGE-M3, E5, or a
  future model — no code change.

## Serve BGE-M3

```bash
docker run -p 8080:80 ghcr.io/huggingface/text-embeddings-inference:latest \
  --model-id BAAI/bge-m3
# backend env:
EMBEDDINGS_PROVIDER=bge-m3
EMBEDDINGS_URL=http://localhost:8080
```

## Build the index

```bash
# One-off / after a bulk import (idempotent — content-hashed, re-embeds only
# changed nodes). Also cron/pg-boss-triggerable:
curl -X POST localhost:8000/api/v1/admin/jobs/reindex-embeddings
```

## Optional: pgvector for fast GLOBAL search (prod scale)

The Python cosine path is fine for the graph-narrowed candidate set. When
the *no-anchor global* search grows large, add an ANN index on a `vector`
column where the extension exists (Neon has it native). This is a
non-breaking upgrade — `float8[]` stays the source of truth. Run once,
out-of-band (NOT in the portable migration chain, so `alembic upgrade head`
stays green on databases without the extension):

```sql
CREATE EXTENSION IF NOT EXISTS vector;
ALTER TABLE ontology_embedding ADD COLUMN embedding_v vector(1024);
UPDATE ontology_embedding SET embedding_v = embedding::vector;      -- backfill
CREATE INDEX ix_ontology_embedding_hnsw
  ON ontology_embedding USING hnsw (embedding_v vector_cosine_ops);
```
Then swap the global-FTS-fallback ranking to use `embedding_v <=> :q` for
ANN candidate generation. The graph-narrowed path is unchanged.
```
