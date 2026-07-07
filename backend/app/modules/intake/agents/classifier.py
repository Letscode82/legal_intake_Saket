"""Deterministic intake classifier — the spine, NOT an agent.

Classification, priority, and SLA math are mechanical Python. No LLM is
involved: the same input always yields the same category/priority/SLA, which
is what makes the pipeline auditable and cheap. LLM agents exist only where
reasoning over a document truly requires it (the NDA/contract/vendor/etc.
agents) — never for a step a regex can do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Ordered, most-specific first. Each rule maps a keyword pattern to a
# canonical request type + category + default priority + SLA hours.
_RULES: list[tuple[re.Pattern[str], dict]] = [
    (
        re.compile(r"\bnda\b|non.?disclosure|mutual.{0,5}confidential", re.I),
        {"type": "NDA Request", "category": "nda", "priority": "Medium", "sla_hours": 24},
    ),
    (
        re.compile(r"vendor|supplier|procure|onboard.{0,10}(vendor|supplier)", re.I),
        {"type": "Vendor Intake", "category": "vendor", "priority": "Medium", "sla_hours": 48},
    ),
    (
        re.compile(r"trademark|™|®|brand name|logo clearance", re.I),
        {"type": "Trademark", "category": "trademark", "priority": "Low", "sla_hours": 72},
    ),
    (
        re.compile(r"litigation|lawsuit|subpoena|demand letter|dispute|breach|violat", re.I),
        {"type": "Litigation", "category": "litigation", "priority": "High", "sla_hours": 8},
    ),
    (
        re.compile(r"contract review|review.{0,10}(agreement|msa|sow)|redline", re.I),
        {"type": "Contract Review", "category": "contract-review", "priority": "Medium", "sla_hours": 48},
    ),
    (
        re.compile(r"policy|handbook|guideline|compliance question", re.I),
        {"type": "Policy Question", "category": "policy", "priority": "Low", "sla_hours": 72},
    ),
]

_URGENT = re.compile(r"\burgent\b|asap|immediately|critical|today|end of day|eod", re.I)


@dataclass
class Classification:
    type: str
    category: str
    priority: str
    sla_hours: int
    matched: bool


def classify_intake(description: str, *, hint_type: str | None = None) -> Classification:
    """Deterministically classify an intake description.

    ``hint_type`` (a form-selected request type) wins when present; otherwise
    the first matching keyword rule applies, else a generic fallback.
    """
    text = description or ""

    result: dict | None = None
    if hint_type:
        for _, rule in _RULES:
            if rule["type"].lower() == hint_type.lower():
                result = dict(rule)
                break
    if result is None:
        for pattern, rule in _RULES:
            if pattern.search(text):
                result = dict(rule)
                break

    matched = result is not None
    if result is None:
        result = {
            "type": hint_type or "General Inquiry",
            "category": "general",
            "priority": "Medium",
            "sla_hours": 48,
        }

    # Urgency escalates priority deterministically.
    if _URGENT.search(text) and result["priority"] in {"Low", "Medium"}:
        result["priority"] = "High"
        result["sla_hours"] = min(result["sla_hours"], 8)

    return Classification(
        type=result["type"],
        category=result["category"],
        priority=result["priority"],
        sla_hours=result["sla_hours"],
        matched=matched,
    )
