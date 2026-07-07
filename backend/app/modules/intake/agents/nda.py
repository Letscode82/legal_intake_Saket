"""NDA Agent — drafts a standard mutual/one-way NDA response from playbook.

Uses Claude for the drafted response when a key is configured; falls back to
a deterministic playbook template (via ``build_degraded_rec``) when the model
is unavailable. Either way it only RECOMMENDS — a human approves.
"""

from __future__ import annotations

import re

from app.core.ai import AIUnavailableError, call_claude_json, friendly_ai_error
from app.modules.intake.agents.base import (
    Recommendation,
    TicketContext,
    build_degraded_rec,
    build_rec,
)

_COUNTERPARTY_RE = re.compile(
    r"(?:with|for)\s+([A-Z][A-Za-z0-9& ]{2,40}?)(?:\s+(?:re\.|regarding|for|by|,|\.|\n)|$)"
)
_TEMPLATE_ID = "MNDA-v4.2"


class NDAAgent:
    id = "nda-agent"
    name = "NDA Agent"
    short_name = "NDA"
    production_ready = True
    description = (
        "Drafts standard mutual & one-way NDAs from playbook templates. "
        "Checks for a prior NDA with the counterparty; recommends template reuse."
    )

    def can_handle(self, ticket: TicketContext) -> bool:
        cat = (ticket.category or "").lower()
        typ = (ticket.type or "").lower()
        desc = (ticket.description or "").lower()
        if "nda" in cat or "nda" in typ:
            return True
        looks_like_nda = re.search(r"\bnda\b|non.?disclosure|mutual.{0,5}confidential", desc)
        # A dispute/breach mention routes to litigation, not NDA drafting.
        return bool(looks_like_nda and not re.search(r"breach|violat", desc))

    async def process(self, ticket: TicketContext) -> Recommendation:
        match = _COUNTERPARTY_RE.search(ticket.description or "")
        counterparty = match.group(1).strip() if match else None
        first_name = (ticket.requester_name or "").split(" ")[0] or "there"

        citations: list[dict] = [
            {"id": _TEMPLATE_ID, "title": "Standard Mutual NDA Template"}
        ]

        try:
            prompt = f"""You are the NDA Agent for AEGIS Legal Mission Control. A legal intake ticket requests a Non-Disclosure Agreement.

TICKET:
- Requester: {ticket.requester_name} ({ticket.department or "Unknown dept"})
- Description: "{ticket.description}"
- Extracted counterparty: {counterparty or "NOT FOUND — ask requester"}

PLAYBOOK TEMPLATE: {_TEMPLATE_ID} (2-year term, standard carve-outs, mutual no-solicit 12 months, Delaware law).

Draft a professional, confident response (as if from a senior paralegal) confirming what you've done and next steps. Mention the template version, key terms, and say the doc is ready for signature. Address the requester by first name. 130-180 words.

Also produce a one-sentence alternative tone (shorter, more casual).

Respond with ONLY this JSON:
{{"draftedResponse":"full response text with \\n line breaks","alternativeTone":"one-line shorter version","confidence":0.92,"reasoning":"one-line why this recommendation is safe","concerns":["any concerns the attorney should see, or empty array"]}}"""

            result = await call_claude_json(prompt, max_tokens=700)
            return build_rec(
                self.id,
                confidence=float(result.get("confidence", 0.92)),
                suggested_action="approve-and-send",
                drafted_response=result.get("draftedResponse", ""),
                reasoning=result.get("reasoning")
                or f"Template-fit match ({_TEMPLATE_ID}).",
                concerns=list(result.get("concerns", [])),
                citations=citations,
                short_form_reply=result.get("alternativeTone"),
            )
        except (AIUnavailableError, Exception) as exc:  # noqa: BLE001
            with_cp = f" with {counterparty}" if counterparty else ""
            fallback = (
                f"Hi {first_name},\n\n"
                f"I've drafted a Standard Mutual NDA{with_cp} using our approved "
                f"template ({_TEMPLATE_ID}):\n\n"
                "• 2-year confidentiality, standard carve-outs\n"
                "• Mutual no-solicit (12 months)\n"
                "• Delaware law, standard venue\n\n"
                "Ready for signature. Reply if you need edits.\n\n"
                "— AEGIS Legal (auto-drafted)"
            )
            return build_degraded_rec(
                self.id,
                drafted_response=fallback,
                reasoning=(
                    "Template-fit match. Claude unavailable — surfaced the playbook "
                    "template for attorney review (not auto-send)."
                ),
                concerns=[
                    friendly_ai_error(exc),
                    "Using template text — attorney must review and personalize before sending.",
                ],
                citations=citations,
            )
