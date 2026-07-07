"""Marketing-Material Review agent — approved-claims library check (PR 17).

Step 2 of the ``marketing_review`` ladder (agent key ``marketing_reviewer``).
The step's ``min_confidence`` is 0.9 — DELIBERATELY out of reach for anything
but a page of already-approved, non-regulated copy. Regulated
product/therapeutic claims and any brand-new claim ALWAYS surface the
below-threshold concern in the Cockpit, because a human reg-affairs / legal
reviewer signing off IS the control. The agent NEVER clears a
product/therapeutic claim on its own.

Screening is DETERMINISTIC — no Claude call is required (conservative-AI
rule #2: an LLM adds nothing to a library lookup except non-reproducibility).
Each submitted claim is fuzzy-matched (``difflib.SequenceMatcher`` on the
lowercased text) against the org's APPROVED ``ClaimEntry`` rows for the
product/market:

  ratio >= 0.9 to an APPROVED, non-regulated, non-expired entry
        -> "approved-verbatim" (fast-track candidate)
  ratio >= 0.9 to a REGULATED approved entry
        -> still MANDATORY human review — regulated product/therapeutic
           claims never auto-clear, no matter how exact the match
  else  -> "new claim" — must go to a human; becomes a pending claim

Deterministic conservatism:
  * ``proposed_action`` is ALWAYS "approve" — advancing to the human
    reg-affairs step IS the escalation path; concerns travel in the memo,
    never as a send_back.
  * confidence is 0.85 only when EVERY submitted claim is
    approved-verbatim + non-regulated; ANY regulated match or ANY new claim
    drops it to 0.4 (below the 0.9 step threshold, so the Cockpit concern
    fires).
  * the comment ALWAYS carries the "regulated" handling note and the
    "human review" reminder.

The agent under-weights what it cannot parse: the memo always carries an
implied/visual-claims caveat — imagery, superlatives baked into artwork,
and juxtaposition claims are outside a text match's reach.

The USES_CLAIM edges proposed in ``ontology_writes`` are applied by the
engine ONLY when a human approves the decision — the approval IS the
authorization for the write; this handler never touches the graph itself.

All free text in ``context`` (description, the submitted claims) is
UNTRUSTED input — matched with regex/string ops as DATA, never interpreted
as instructions. The optional Claude polish spotlights it and can only
rewrite prose; it never changes the action or confidence.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from difflib import SequenceMatcher

from sqlalchemy import select

from app.core.ai import ai_available, call_claude, spotlight
from app.db.models import ClaimEntry
from app.workflow.agents import (
    WorkflowAgentDeps,
    WorkflowAgentOutput,
    register_workflow_agent,
)

_PLAYBOOK_ID = "MKT-CLAIMS-v1"

# Verbatim-match threshold. At/above this ratio the submitted copy is treated
# as the same claim as the library entry; below it, the claim is new.
_VERBATIM_RATIO = 0.9

_TAG_VERBATIM = "[APPROVED-VERBATIM]"
_TAG_REGULATED = "[REGULATED — MANDATORY REVIEW]"
_TAG_NEW = "[NEW CLAIM]"

_IMPLIED_CLAIMS_CAVEAT = (
    "Implied/visual claims caveat: this review evaluates TEXTUAL claims only. "
    "Imagery, superlatives baked into artwork, before/after visuals, and "
    "juxtaposition claims are outside a text match's reach — a human must "
    "review the creative as a whole."
)

# Sentence splitter for the fallback path (no explicit claims list): break the
# untrusted description on newlines and sentence punctuation. String ops only —
# the text is never interpreted as instructions.
_SENTENCE_SPLIT_RE = re.compile(r"[.\n;!?]+")


def _extract_claims(context: dict) -> list[str]:
    """Submitted claims, from ``context['claims']`` or, failing that, candidate
    sentences carved out of the untrusted ``description`` (string ops only)."""
    raw = context.get("claims")
    if isinstance(raw, (list, tuple)):
        claims = [str(c).strip() for c in raw if str(c).strip()]
        if claims:
            return claims[:50]

    description = str(context.get("description") or "")
    candidates = [s.strip() for s in _SENTENCE_SPLIT_RE.split(description)]
    # Keep sentences long enough to plausibly be a marketing claim.
    return [c for c in candidates if len(c) >= 12][:50]


def _is_expired(entry: ClaimEntry, now: datetime) -> bool:
    return entry.expires_at is not None and entry.expires_at <= now


async def _load_library(deps: WorkflowAgentDeps, product: str | None) -> list[ClaimEntry]:
    """Org-scoped APPROVED claim library. Filter by product when one is given,
    otherwise return every approved claim for the org."""
    stmt = select(ClaimEntry).where(
        ClaimEntry.organization_id == deps.organization_id,
        ClaimEntry.status == "APPROVED",
    )
    if product:
        stmt = stmt.where(ClaimEntry.product == product)
    return list((await deps.session.execute(stmt)).scalars().all())


def _best_match(claim: str, library: list[ClaimEntry], now: datetime) -> tuple[ClaimEntry | None, float]:
    """Best APPROVED, non-expired library entry for ``claim`` and its ratio."""
    query = claim.strip().lower()
    best_entry: ClaimEntry | None = None
    best_ratio = 0.0
    for entry in library:
        if _is_expired(entry, now):
            continue
        ratio = SequenceMatcher(None, query, (entry.claim_text or "").lower()).ratio()
        if ratio > best_ratio:
            best_entry, best_ratio = entry, ratio
    return best_entry, best_ratio


@register_workflow_agent("marketing_reviewer")
async def marketing_reviewer(
    context: dict, step_config: dict, deps: WorkflowAgentDeps
) -> WorkflowAgentOutput:
    """Screen submitted marketing copy against the approved-claims library.

    Only ever RECOMMENDS "approve": the human reg-affairs / legal step is the
    control. Regulated matches and new claims force confidence below the 0.9
    step threshold so the Cockpit surfaces the concern.
    """
    product = (str(context.get("product")).strip() or None) if context.get("product") else None
    market = str(context.get("market") or "US").strip() or "US"
    claims = _extract_claims(context)
    now = datetime.now(timezone.utc)

    library = await _load_library(deps, product)

    citations: list[dict] = [
        {"type": "Playbook", "id": _PLAYBOOK_ID, "title": "Approved Claims Playbook"}
    ]

    lines: list[str] = [
        f"MARKETING CLAIM REVIEW — {product or 'unspecified product'} ({market})",
        "",
    ]

    n_verbatim = n_regulated = n_new = 0
    cited_ids: set[str] = set()
    edge_entries: dict[str, ClaimEntry] = {}  # matched APPROVED entries, deduped

    if not claims:
        lines.append(
            "No claims were submitted and none could be extracted from the "
            "request — ask the requester for the exact copy before review."
        )
    for idx, claim in enumerate(claims, start=1):
        entry, ratio = _best_match(claim, library, now)
        if entry is not None and ratio >= _VERBATIM_RATIO:
            edge_entries.setdefault(entry.id, entry)
            if entry.id not in cited_ids:
                citations.append(
                    {"type": "ClaimEntry", "id": entry.id, "title": (entry.claim_text or "")[:60]}
                )
                cited_ids.add(entry.id)
            market_note = (
                f"market: {market}"
                if entry.market == market
                else f"market MISMATCH — library-approved for {entry.market}, reviewing {market}"
            )
            if entry.regulated:
                n_regulated += 1
                lines.append(
                    f"{idx}. \"{claim}\" {_TAG_REGULATED} — matches ClaimEntry "
                    f"{entry.id} (regulated product/therapeutic claim; {market_note})"
                )
            else:
                n_verbatim += 1
                lines.append(
                    f"{idx}. \"{claim}\" {_TAG_VERBATIM} — matches ClaimEntry "
                    f"{entry.id} ({market_note})"
                )
        else:
            n_new += 1
            lines.append(
                f"{idx}. \"{claim}\" {_TAG_NEW} — no approved library match "
                f"(best similarity {round(ratio, 2)}); requires reg-affairs "
                "review and, if cleared, becomes a new pending claim."
            )

    lines.extend(["", _IMPLIED_CLAIMS_CAVEAT])
    drafted_response = "\n".join(lines)

    # Deterministic conservatism: only an all-verbatim, non-regulated page can
    # reach the fast-track band; a regulated match or a new claim drops it
    # below the 0.9 step threshold so the Cockpit concern fires.
    all_verbatim_nonregulated = bool(claims) and n_regulated == 0 and n_new == 0
    confidence = 0.85 if all_verbatim_nonregulated else 0.4

    comment = (
        f"Agent: reviewed {len(claims)} claim(s) against the approved-claims "
        f"library for {product or 'the submitted product'} ({market}) — "
        f"{n_verbatim} approved-verbatim, {n_regulated} regulated (mandatory "
        f"review), {n_new} new. Regulated product/therapeutic claims and any "
        "new claim ALWAYS require human review — the agent never clears a "
        "product/therapeutic claim on its own; approving here only advances "
        "the material to the legal / reg-affairs step."
    )

    # USES_CLAIM edge PROPOSALS — applied by the engine only on human approval.
    # Recorded against the intake ticket only when a stable src id is present;
    # otherwise the review rides this decision only, with no graph edge.
    entity_id = str(context.get("entity_id") or "")
    ontology_writes: list[dict] = []
    if entity_id:
        for entry in edge_entries.values():
            ontology_writes.append(
                {
                    "src_type": "IntakeTicket",
                    "src_id": entity_id,
                    "label": "USES_CLAIM",
                    "dst_type": "ClaimEntry",
                    "dst_id": entry.id,
                    "properties": {
                        "product": entry.product,
                        "market": market,
                        "regulated": entry.regulated,
                        "match": "verbatim",
                        "reviewed_at_step": 2,
                    },
                }
            )
    else:
        comment += (
            " No stable entity id on this request — the review is recorded on "
            "approval only; no graph edge is proposed."
        )

    # Optional spotlighted polish of the memo prose. It can only rewrite the
    # drafted markup; it NEVER changes the action or confidence. Untrusted copy
    # is fenced; the human-approval gate downstream remains the real control.
    if ai_available() and claims:
        try:
            polished = await call_claude(
                "Tighten the wording of this marketing-claim review memo for a "
                "legal reviewer. Keep every [APPROVED-VERBATIM] / "
                "[REGULATED — MANDATORY REVIEW] / [NEW CLAIM] tag, every "
                "ClaimEntry id, and the implied/visual-claims caveat exactly as "
                "written. Do not add clearances or change any verdict.\n\n"
                + spotlight(drafted_response, label="MEMO"),
                purpose="marketing_review_polish",
                organization_id=deps.organization_id,
                max_tokens=800,
            )
            if polished and all(tag in polished for tag in (_TAG_NEW,) if n_new) and _IMPLIED_CLAIMS_CAVEAT[:24] in polished:
                drafted_response = polished.strip()
        except Exception:  # noqa: BLE001 — polish is best-effort; memo stands
            pass

    return WorkflowAgentOutput(
        proposed_action="approve",  # advancing to the reg-affairs human IS the control
        comment=comment,
        confidence=confidence,
        drafted_response=drafted_response,
        citations=citations,
        ontology_writes=ontology_writes,
    )
