"""Ontology edge helpers — the write/read surface for typed links.

Edges are AUTHORED by modules as a byproduct of normal legal work (the NDA
approval writes NDA_WITH; the screening approval writes SCREENED_ON) — never
extracted by an LLM. ``add_edge`` is idempotent on the edge identity so a
re-approved or re-run mutation never duplicates a link; re-adding refreshes
the properties instead.

Auditing: edge writes ride the parent mutation's audit row (they are part of
the approved action), so this module doesn't write its own.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OntologyEdge


@dataclass(frozen=True)
class NodeRef:
    """Polymorphic reference to a shared-entity node."""

    type: str  # "Counterparty", "Person", "Document", "Contract", …
    id: str


async def add_edge(
    session: AsyncSession,
    *,
    organization_id: str,
    src: NodeRef,
    label: str,
    dst: NodeRef,
    properties: dict | None = None,
    source_module: str,
    created_by: str | None = None,
) -> OntologyEdge:
    """Idempotently upsert one typed edge. No commit — composes into the
    caller's mutation transaction."""
    existing = (
        await session.execute(
            select(OntologyEdge).where(
                OntologyEdge.organization_id == organization_id,
                OntologyEdge.src_type == src.type,
                OntologyEdge.src_id == src.id,
                OntologyEdge.label == label,
                OntologyEdge.dst_type == dst.type,
                OntologyEdge.dst_id == dst.id,
            )
        )
    ).scalars().first()
    if existing is not None:
        if properties:
            existing.properties = {**(existing.properties or {}), **properties}
        return existing

    edge = OntologyEdge(
        organization_id=organization_id,
        src_type=src.type,
        src_id=src.id,
        label=label,
        dst_type=dst.type,
        dst_id=dst.id,
        properties=properties or {},
        source_module=source_module,
        created_by=created_by,
    )
    session.add(edge)
    await session.flush()
    return edge


async def get_neighbors(
    session: AsyncSession,
    *,
    organization_id: str,
    node: NodeRef,
    labels: list[str] | None = None,
    direction: str = "both",  # "out" | "in" | "both"
) -> list[OntologyEdge]:
    """1-hop neighborhood of ``node``. The k-hop recursive-CTE traversal
    (permission-filtered) lands with the GraphRAG service (PR 4); this is the
    primitive it builds on and what module code uses directly."""
    clauses = []
    if direction in ("out", "both"):
        stmt = select(OntologyEdge).where(
            OntologyEdge.organization_id == organization_id,
            OntologyEdge.src_type == node.type,
            OntologyEdge.src_id == node.id,
        )
        if labels:
            stmt = stmt.where(OntologyEdge.label.in_(labels))
        clauses.append(stmt)
    if direction in ("in", "both"):
        stmt = select(OntologyEdge).where(
            OntologyEdge.organization_id == organization_id,
            OntologyEdge.dst_type == node.type,
            OntologyEdge.dst_id == node.id,
        )
        if labels:
            stmt = stmt.where(OntologyEdge.label.in_(labels))
        clauses.append(stmt)

    edges: dict[str, OntologyEdge] = {}
    for stmt in clauses:
        for edge in (await session.execute(stmt)).scalars():
            edges[edge.id] = edge
    return list(edges.values())
