"""FAQ + Policy Q&A agents — quote-only answers over approved knowledge.

Both agents retrieve exclusively from ``KnowledgeEntry`` rows that are
``is_current`` (the approved corpus) and answer by QUOTING the entry body —
never by free-form legal reasoning. Search is Postgres FTS expressed through
SQLAlchemy Core / ORM constructs only (``func.to_tsvector`` / ``to_tsquery``
/ ``ts_rank``) — raw ``text()`` SQL stays confined to ``app/db/**``.

Discipline per agent:

* FAQ (``answer_faq``): a deterministic escalation regex runs BEFORE any
  answering — dispute/lawsuit/regulator/deadline/subpoena/breach-of-contract
  questions ALWAYS hand off to the legal queue regardless of retrieval
  score. Otherwise the top entry's body is the answer (Claude may rephrase
  it conversationally; on AI unavailability the verbatim body ships), always
  with the not-legal-advice framing line and an exact (entry, version)
  citation.

* Policy (``answer_policy``): quote-or-cite means QUOTE — the body ships
  verbatim, no rephrasing ever. If the top two current policies share a
  topic and score within 30% of each other, the agent refuses to harmonize:
  it returns a conflict hand-off citing both and records a
  ``knowledge.policy_conflict`` Event.

Every delivered answer (and every conflict) writes an AnswerRecord: one
``Event`` row plus a CITES ontology edge per cited entry, committed here.
"""

from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai import AIUnavailableError, call_claude, spotlight
from app.core.security import Actor
from app.db.models import Event, KnowledgeEntry
from app.db.ontology import NodeRef, add_edge

# Same tokenizer discipline as app/db/graphrag.py: stopword-filtered
# [A-Za-z0-9]{3,} words, OR-joined into a to_tsquery expression.
_STOPWORDS = {
    "the", "and", "with", "for", "have", "has", "was", "are", "our", "any",
    "this", "that", "there", "does", "did", "can", "could", "should", "would",
    "please", "need", "want", "about", "from", "into", "what", "which", "who",
    "when", "where", "how", "why", "you", "your", "not",
}

# Below this ts_rank the agent hands off rather than guess. A single-token
# incidental match ranks ~0.06; real hits on 2-3 tokens land well above.
_SCORE_THRESHOLD = 0.05

# Two same-topic current policies whose scores are within 30% of each other
# are a conflict — the agent never harmonizes contradicting policies.
_CONFLICT_SCORE_RATIO = 0.7

# Deterministic escalation — checked BEFORE answering, wins over any score.
_ESCALATION_RE = re.compile(
    r"dispute|lawsuit|regulator|deadline|subpoena|breach of contract",
    re.IGNORECASE,
)

FRAMING_NOTE = (
    "This is general guidance from the approved knowledge base, "
    "not legal advice for your specific facts."
)

_HANDOFF = {
    "hint": "file via /api/v1/intake/front-door",
    "suggested_request_type": "contract",
}


def _tokens(question: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9]{3,}", question.lower())
    return [w for w in words if w not in _STOPWORDS][:12]


def _or_tsquery(tokens: list[str]) -> str:
    return " | ".join(tokens)


async def search_entries(
    session: AsyncSession,
    org_id: str,
    kind: str,
    question: str,
) -> list[tuple[KnowledgeEntry, float]]:
    """FTS over title+body of current entries of ``kind`` in ``org_id``.

    Pure SQLAlchemy Core/ORM expressions — the tsvector/tsquery/ts_rank
    calls are ``func.*`` constructs, so no raw SQL leaves app/db/**.
    Returns (entry, score) pairs, best first.
    """
    tokens = _tokens(question)
    if not tokens:
        return []

    tsv = func.to_tsvector(
        "english", KnowledgeEntry.title + " " + KnowledgeEntry.body
    )
    tsq = func.to_tsquery("english", _or_tsquery(tokens))
    score = func.ts_rank(tsv, tsq).label("score")

    stmt = (
        select(KnowledgeEntry, score)
        .where(
            KnowledgeEntry.organization_id == org_id,
            KnowledgeEntry.kind == kind,
            KnowledgeEntry.is_current.is_(True),
            tsv.op("@@")(tsq),
        )
        .order_by(score.desc(), KnowledgeEntry.slug)
        .limit(10)
    )
    rows = (await session.execute(stmt)).all()
    return [(row[0], float(row[1])) for row in rows]


def _citation(entry: KnowledgeEntry) -> dict:
    return {
        "type": "KnowledgeEntry",
        "id": entry.id,
        "slug": entry.slug,
        "title": entry.title,
        "version": entry.version,
    }


def _policy_citation(entry: KnowledgeEntry) -> dict:
    return {
        **_citation(entry),
        "policy_name": entry.title,
        "effective_date": (
            entry.effective_date.isoformat() if entry.effective_date else None
        ),
    }


def _handoff_result(reason: str | None = None) -> dict:
    handoff = dict(_HANDOFF)
    if reason:
        handoff["reason"] = reason
    return {
        "answered": False,
        "answer": None,
        "conflict": False,
        "handoff": handoff,
        "citations": [],
    }


async def _write_answer_record(
    session: AsyncSession,
    *,
    actor: Actor,
    question: str,
    event_type: str,
    kind: str,
    entries: list[KnowledgeEntry],
    extra_payload: dict | None = None,
) -> Event:
    """AnswerRecord: one Event row + a CITES edge per cited entry. Commits."""
    primary = entries[0]
    event = Event(
        organization_id=actor.organization_id,
        type=event_type,
        source_type="KnowledgeEntry",
        source_id=primary.id,
        actor_id=actor.user_id,
        summary=question[:200],
        payload={"version": primary.version, "kind": kind, **(extra_payload or {})},
    )
    session.add(event)
    await session.flush()

    for entry in entries:
        await add_edge(
            session,
            organization_id=actor.organization_id,
            src=NodeRef("Event", event.id),
            label="CITES",
            dst=NodeRef("KnowledgeEntry", entry.id),
            properties={"version": entry.version},
            source_module="knowledge",
            created_by=actor.user_id,
        )
    await session.commit()
    return event


async def answer_faq(
    session: AsyncSession,
    actor: Actor,
    question: str,
) -> dict:
    """FAQ agent — the one direct-answer agent, allowed because it can only
    quote approved content. Escalation regex wins over everything."""
    if _ESCALATION_RE.search(question):
        return _handoff_result(reason="routed to the legal queue")

    results = await search_entries(
        session, actor.organization_id, "FAQ", question
    )
    if not results or results[0][1] < _SCORE_THRESHOLD:
        return _handoff_result()

    entry, _score = results[0]

    # Claude may REPHRASE the approved body conversationally — never add to
    # it. Question and body are both untrusted-input spotlighted; on AI
    # unavailability the verbatim body ships.
    answer_body = entry.body
    try:
        prompt = (
            "Rephrase the approved knowledge-base entry below as a short, "
            "conversational answer to the employee's question. Use ONLY "
            "facts stated in the entry — do not add, infer, or drop any "
            "guidance, and keep every number, timeframe, and dollar "
            "threshold exactly as written.\n\n"
            f"{spotlight(question, 'QUESTION')}\n\n"
            f"{spotlight(entry.body, 'APPROVED_ENTRY')}"
        )
        answer_body = await call_claude(
            prompt,
            max_tokens=400,
            purpose="agent.faq",
            organization_id=actor.organization_id,
        )
    except AIUnavailableError:
        answer_body = entry.body

    await _write_answer_record(
        session,
        actor=actor,
        question=question,
        event_type="knowledge.answer",
        kind="FAQ",
        entries=[entry],
    )

    return {
        "answered": True,
        "answer": f"{answer_body}\n\n{FRAMING_NOTE}",
        "conflict": False,
        "handoff": None,
        "citations": [_citation(entry)],
    }


async def answer_policy(
    session: AsyncSession,
    actor: Actor,
    question: str,
) -> dict:
    """Policy agent — quote-or-cite. The body ships VERBATIM (no rephrasing);
    same-topic conflicts route to the policy owner, never harmonized."""
    results = await search_entries(
        session, actor.organization_id, "POLICY", question
    )
    if not results or results[0][1] < _SCORE_THRESHOLD:
        return _handoff_result()

    if len(results) >= 2:
        (top, top_score), (second, second_score) = results[0], results[1]
        same_topic = top.topic is not None and top.topic == second.topic
        if same_topic and second_score >= top_score * _CONFLICT_SCORE_RATIO:
            await _write_answer_record(
                session,
                actor=actor,
                question=question,
                event_type="knowledge.policy_conflict",
                kind="POLICY",
                entries=[top, second],
                extra_payload={
                    "topic": top.topic,
                    "conflicting_entries": [
                        {"id": e.id, "slug": e.slug, "version": e.version}
                        for e in (top, second)
                    ],
                },
            )
            return {
                "answered": False,
                "answer": "Two policies apply — routed to the policy owner.",
                "conflict": True,
                "handoff": None,
                "citations": [_policy_citation(top), _policy_citation(second)],
            }

    entry, _score = results[0]
    await _write_answer_record(
        session,
        actor=actor,
        question=question,
        event_type="knowledge.answer",
        kind="POLICY",
        entries=[entry],
    )

    return {
        "answered": True,
        "answer": f"{entry.body}\n\n{FRAMING_NOTE}",
        "conflict": False,
        "handoff": None,
        "citations": [_policy_citation(entry)],
    }
