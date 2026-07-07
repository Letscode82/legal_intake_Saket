"""GraphRAG retrieval — the one read path every agent and the Brain share.

Pipeline (per the working-architecture doc):
  natural-language query
    → entity-link to graph ANCHORS (FTS over named entities)
    → PERMISSION-FILTERED k-hop traversal of ontology_edge (recursive CTE)
    → candidate subgraph
    → hybrid text ranking within it (Postgres FTS today; vector fusion
      activates with pgvector + an embedding provider — see
      core/embeddings.py)
    → compact, CITED context
    → explicit GAP NOTES (what the record does not contain).

Lives in the db package: the recursive CTEs here are the sanctioned home
for raw SQL outside migrations (same rule as the audit-chain helpers).

Permission model: every hit type maps to the read permission that governs
it; nodes the actor cannot read are excluded BEFORE hydration, so neither
citations nor counts leak their existence. "No agent has a private data
path" — agents and humans both come through here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission
from app.core.security import Actor

_STOPWORDS = {
    "the", "and", "with", "for", "have", "has", "was", "are", "our", "any",
    "this", "that", "there", "does", "did", "can", "could", "should", "would",
    "please", "need", "want", "about", "from", "into", "what", "which", "who",
    "when", "where", "how", "why", "you", "your", "not",
}

# Which read permission governs each node type. Nodes whose permission the
# actor lacks are dropped before hydration. Coarse v1 map — per-owner
# document gating refines when @aegis-style document ACLs land.
_TYPE_PERMISSION: dict[str, Permission] = {
    "IntakeTicket": Permission.INTAKE_READ_ALL_TICKETS,
    "Document": Permission.CONTRACTS_READ_ALL,
    "Counterparty": Permission.MATTER_READ_ALL,
    "Person": Permission.MATTER_READ_ALL,
    "Organization": Permission.MATTER_READ_ALL,
}


@dataclass
class GraphHit:
    type: str
    id: str
    title: str
    snippet: str
    score: float
    hops: int  # 0 = anchor, -1 = global-FTS fallback (no graph path)
    via_labels: list[str] = field(default_factory=list)


@dataclass
class GraphRAGResult:
    query: str
    anchors: list[GraphHit]
    hits: list[GraphHit]
    gap_notes: list[str]
    subgraph_node_count: int
    retrieval_mode: str  # "graph+fts" | "fts-only"


def _tokens(query: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9]{3,}", query.lower())
    return [w for w in words if w not in _STOPWORDS][:12]


def _or_tsquery(tokens: list[str]) -> str:
    return " | ".join(tokens)


def _allowed_types(actor: Actor) -> set[str]:
    return {
        node_type
        for node_type, perm in _TYPE_PERMISSION.items()
        if perm.value in actor.permissions
    }


async def retrieve(
    session: AsyncSession,
    actor: Actor,
    query: str,
    *,
    k_hops: int = 2,
    limit: int = 12,
) -> GraphRAGResult:
    tokens = _tokens(query)
    gap_notes: list[str] = []
    allowed = _allowed_types(actor)

    if not tokens:
        return GraphRAGResult(
            query=query, anchors=[], hits=[],
            gap_notes=["The question contained no searchable terms."],
            subgraph_node_count=0, retrieval_mode="fts-only",
        )
    if not allowed:
        return GraphRAGResult(
            query=query, anchors=[], hits=[],
            gap_notes=[
                "Your role has no read access to the record types the Brain "
                "searches — results are permission-filtered."
            ],
            subgraph_node_count=0, retrieval_mode="fts-only",
        )

    tsq = _or_tsquery(tokens)

    # ── 1. Anchor resolution: FTS over named entities ────────────────
    anchors = await _find_anchors(session, actor.organization_id, tsq, allowed)

    # ── 2. Permission-filtered k-hop traversal from the anchors ──────
    reached: dict[tuple[str, str], dict] = {}
    if anchors:
        reached = await _k_hop(
            session, actor.organization_id,
            [(a.type, a.id) for a in anchors], k_hops,
        )
        # Drop nodes the actor cannot read BEFORE hydration.
        reached = {
            key: meta for key, meta in reached.items() if key[0] in allowed
        }
    else:
        gap_notes.append(
            "No known entity in the record matched the question — falling "
            "back to full-text search without graph context."
        )

    # ── 3. Hydrate + rank candidates ─────────────────────────────────
    hits: list[GraphHit] = []
    if reached:
        hits = await _hydrate_and_rank(
            session, actor.organization_id, reached, tsq
        )
        mode = "graph+fts"
    else:
        # Global FTS fallback (still permission-filtered).
        hits = await _global_fts(session, actor.organization_id, tsq, allowed)
        mode = "fts-only"
        if anchors and not hits:
            gap_notes.append(
                "Anchors resolved but their neighborhood contained nothing "
                "your role can read."
            )

    hits = hits[:limit]
    if not hits:
        gap_notes.append(
            "The record contains nothing matching this question. Absence "
            "of records is not evidence of absence — check systems outside "
            "the platform before relying on this."
        )

    return GraphRAGResult(
        query=query,
        anchors=anchors,
        hits=hits,
        gap_notes=gap_notes,
        subgraph_node_count=len(reached),
        retrieval_mode=mode,
    )


async def _find_anchors(
    session: AsyncSession, org_id: str, tsq: str, allowed: set[str]
) -> list[GraphHit]:
    """Top named-entity matches — the graph entry points."""
    sql_parts = []
    if "Counterparty" in allowed:
        sql_parts.append(
            """
            SELECT 'Counterparty' AS type, id, name AS title,
                   ts_rank(to_tsvector('english', name), q) AS rank
            FROM counterparty, to_tsquery('english', :tsq) q
            WHERE organization_id = :org
              AND to_tsvector('english', name) @@ q
            """
        )
    if "Person" in allowed:
        sql_parts.append(
            """
            SELECT 'Person' AS type, id,
                   name AS title,
                   ts_rank(to_tsvector('english',
                           coalesce(name,'') || ' ' || coalesce(email,'')), q) AS rank
            FROM person, to_tsquery('english', :tsq) q
            WHERE organization_id = :org
              AND to_tsvector('english',
                  coalesce(name,'') || ' ' || coalesce(email,'')) @@ q
            """
        )
    if "Document" in allowed:
        sql_parts.append(
            """
            SELECT 'Document' AS type, id, name AS title,
                   ts_rank(to_tsvector('english', name), q) AS rank
            FROM document, to_tsquery('english', :tsq) q
            WHERE organization_id = :org
              AND to_tsvector('english', name) @@ q
            """
        )
    if not sql_parts:
        return []
    sql = " UNION ALL ".join(sql_parts) + " ORDER BY rank DESC LIMIT 5"
    rows = (
        await session.execute(text(sql), {"org": org_id, "tsq": tsq})
    ).mappings().all()
    return [
        GraphHit(
            type=r["type"], id=r["id"], title=r["title"],
            snippet="", score=float(r["rank"]), hops=0,
        )
        for r in rows
    ]


async def _k_hop(
    session: AsyncSession,
    org_id: str,
    seeds: list[tuple[str, str]],
    k_hops: int,
) -> dict[tuple[str, str], dict]:
    """Undirected k-hop reach over ontology_edge from the seed nodes.

    Returns {(type, id): {"depth": n, "labels": [...]}}. Edges are walked in
    both directions — the graph is navigational context, not a DAG.
    """
    seed_rows = " UNION ALL ".join(
        f"SELECT CAST(:st{i} AS text), CAST(:si{i} AS text), 0, "
        f"CAST(NULL AS text)"
        for i in range(len(seeds))
    )
    params: dict = {"org": org_id, "k": k_hops}
    for i, (stype, sid) in enumerate(seeds):
        params[f"st{i}"] = stype
        params[f"si{i}"] = sid

    sql = f"""
        WITH RECURSIVE frontier(node_type, node_id, depth, via_label) AS (
            {seed_rows}
            UNION
            SELECT
                CASE WHEN e.src_type = f.node_type AND e.src_id = f.node_id
                     THEN e.dst_type ELSE e.src_type END,
                CASE WHEN e.src_type = f.node_type AND e.src_id = f.node_id
                     THEN e.dst_id ELSE e.src_id END,
                f.depth + 1,
                e.label
            FROM ontology_edge e
            JOIN frontier f
              ON e.organization_id = :org
             AND ((e.src_type = f.node_type AND e.src_id = f.node_id)
               OR (e.dst_type = f.node_type AND e.dst_id = f.node_id))
            WHERE f.depth < :k
        )
        SELECT node_type, node_id, MIN(depth) AS depth,
               ARRAY_REMOVE(ARRAY_AGG(DISTINCT via_label), NULL) AS labels
        FROM frontier
        GROUP BY node_type, node_id
    """
    rows = (await session.execute(text(sql), params)).mappings().all()
    return {
        (r["node_type"], r["node_id"]): {
            "depth": int(r["depth"]),
            "labels": list(r["labels"] or []),
        }
        for r in rows
    }


_HYDRATION_SQL: dict[str, str] = {
    "Counterparty": """
        SELECT id, name AS title,
               'Counterparty (' || type || COALESCE(', ' || country, '') || ')'
                   AS snippet,
               ts_rank(to_tsvector('english', name), q) AS text_rank
        FROM counterparty, to_tsquery('english', :tsq) q
        WHERE organization_id = :org AND id = ANY(:ids)
    """,
    "Person": """
        SELECT id, name AS title,
               'Person (' || type || COALESCE(', ' || email, '') || ')' AS snippet,
               ts_rank(to_tsvector('english',
                       coalesce(name,'') || ' ' || coalesce(email,'')), q)
                   AS text_rank
        FROM person, to_tsquery('english', :tsq) q
        WHERE organization_id = :org AND id = ANY(:ids)
    """,
    "Document": """
        SELECT id, name AS title,
               LEFT(COALESCE(extracted_text, mime_type), 240) AS snippet,
               ts_rank(to_tsvector('english',
                       coalesce(name,'') || ' ' || coalesce(extracted_text,'')), q)
                   AS text_rank
        FROM document, to_tsquery('english', :tsq) q
        WHERE organization_id = :org AND id = ANY(:ids)
    """,
    "IntakeTicket": """
        SELECT id, id || ' · ' || type AS title,
               LEFT(description, 240) AS snippet,
               ts_rank(to_tsvector('english', description), q) AS text_rank
        FROM intake_ticket, to_tsquery('english', :tsq) q
        WHERE organization_id = :org AND id = ANY(:ids)
    """,
}


async def _hydrate_and_rank(
    session: AsyncSession,
    org_id: str,
    reached: dict[tuple[str, str], dict],
    tsq: str,
) -> list[GraphHit]:
    by_type: dict[str, list[str]] = {}
    for (ntype, nid) in reached:
        by_type.setdefault(ntype, []).append(nid)

    hits: list[GraphHit] = []
    for ntype, ids in by_type.items():
        sql = _HYDRATION_SQL.get(ntype)
        if sql is None:
            continue  # types without a hydration shape aren't citable yet
        rows = (
            await session.execute(
                text(sql), {"org": org_id, "ids": ids, "tsq": tsq}
            )
        ).mappings().all()
        for r in rows:
            meta = reached[(ntype, r["id"])]
            depth = meta["depth"]
            # Rank fusion: hop proximity + FTS relevance within the subgraph.
            score = 1.0 / (1 + depth) + 2.0 * float(r["text_rank"] or 0.0)
            hits.append(
                GraphHit(
                    type=ntype, id=r["id"], title=r["title"],
                    snippet=r["snippet"] or "", score=round(score, 4),
                    hops=depth, via_labels=meta["labels"],
                )
            )
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits


async def _global_fts(
    session: AsyncSession, org_id: str, tsq: str, allowed: set[str]
) -> list[GraphHit]:
    """No graph path — permission-filtered FTS across citable tables."""
    hits: list[GraphHit] = []
    for ntype, sql in _HYDRATION_SQL.items():
        if ntype not in allowed:
            continue
        fts_sql = sql.replace(
            "AND id = ANY(:ids)",
            "AND to_tsvector('english', "
            + _FTS_COLUMN[ntype]
            + ") @@ q",
        )
        rows = (
            await session.execute(text(fts_sql), {"org": org_id, "tsq": tsq})
        ).mappings().all()
        for r in rows:
            hits.append(
                GraphHit(
                    type=ntype, id=r["id"], title=r["title"],
                    snippet=r["snippet"] or "",
                    score=round(2.0 * float(r["text_rank"] or 0.0), 4),
                    hops=-1,
                )
            )
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits


_FTS_COLUMN: dict[str, str] = {
    "Counterparty": "name",
    "Person": "coalesce(name,'') || ' ' || coalesce(email,'')",
    "Document": "coalesce(name,'') || ' ' || coalesce(extracted_text,'')",
    "IntakeTicket": "description",
}
