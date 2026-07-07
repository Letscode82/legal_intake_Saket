"""Governance-workflow library — the ladders for a pharma GC office.

One intake, many ladders. Every request that reaches Legal (contract, NDA,
litigation summons, statutory notice, regulator action, vendor onboarding,
whistleblower report, data-breach alert, board approval...) is routed to a
matter-type-specific governance workflow. The library below is grounded in
what a Dr. Reddy's-grade GC office actually handles:

  * ANDA / Para IV patent litigation with the Hatch-Waxman 45-day statutory
    window, and settlement approvals that REQUIRE antitrust review — patent
    settlements have drawn US antitrust suits against generic makers.
  * USFDA actions (Form 483s / warning letters) across globally inspected
    facilities; NPPA/DPCO pricing notices in India.
  * Statutory legal notices with hard reply deadlines.
  * Counterparty/vendor onboarding with sanctions & debarment screening.
  * UCPMP / anti-bribery compliance investigations, DPDP data-breach response
    (72-hour breach-notification clock), POSH/employment matters, board &
    secretarial approvals, and the everyday NDA/contract flow.

Each definition is plain data for the shared workflow engine: human and
agent steps, SLAs (which drive Amber->Red aging and the delay analytics),
skip rules, and escalation thresholds. Nothing here adds engine complexity.

Agent steps name a handler registered below. Handlers only RECOMMEND: their
output is persisted as a PENDING ``AgentDecision`` and never auto-applies —
a human approval in the Cockpit is the only path that moves the ladder
(conservative-AI governance).

``classify()`` is the deterministic spine: keyword triage, mechanical and
auditable — the same input always routes the same way. Routing stays
deterministic by design; LLM reasoning is reserved for the document-review
agent steps, never for routing.
"""

from __future__ import annotations

from app.workflow.agents import (
    WorkflowAgentDeps,
    WorkflowAgentOutput,
    register_workflow_agent,
)

# ---------------------------------------------------------------------------
# The library. step fields: step_order, name, screen_key, approver_role,
# kind ('human'|'agent'), agent_config, sla_hours, metadata (skip_if).
# ---------------------------------------------------------------------------

def _h(order, name, screen, role, sla=None, skip_if=None):
    return {"step_order": order, "name": name, "screen_key": screen,
            "approver_role": role, "kind": "human", "agent_config": {},
            "sla_hours": sla, "metadata": {"skip_if": skip_if} if skip_if else {}}


def _a(order, name, screen, role, agent_key, min_confidence=0.8, sla=None):
    return {"step_order": order, "name": name, "screen_key": screen,
            "approver_role": role, "kind": "agent",
            "agent_config": {"agent_key": agent_key, "min_confidence": min_confidence},
            "sla_hours": sla, "metadata": {}}


WORKFLOW_LIBRARY: list[dict] = [
    {
        "key": "nda_fasttrack", "name": "NDA Fast-Track",
        "description": "Mutual/one-way NDAs. Agent reviews against the standard template; only deviations reach a lawyer.",
        "steps": [
            _h(1, "Request & Upload", "nda_intake", "requester"),
            _a(2, "AI Template Review", "agent_review", "legal_team", "nda_reviewer", 0.75, sla=4),
            _h(3, "Legal Sign-off", "legal_review", "legal_team", sla=24),
            _h(4, "E-Signature", "signature_screen", "signatory", sla=48),
        ],
    },
    {
        "key": "clm_contract_approval", "name": "Contract Approval Ladder",
        "description": "Commercial contracts: supply, distribution, licensing, services.",
        "steps": [
            _h(1, "Draft & Submit", "contract_draft", "contract_owner"),
            _a(2, "AI Risk Review", "agent_review", "legal_team", "contract_risk_reviewer", 0.8, sla=8),
            _h(3, "Legal Review", "legal_review", "legal_team", sla=48),
            _h(4, "Finance Review", "finance_review", "finance_team", sla=48,
               skip_if={"field": "contract_value", "op": "lt", "value": 10000}),
            _h(5, "GC Approval", "gc_approval", "general_counsel", sla=72),
            _h(6, "Counter-signature", "signature_screen", "signatory", sla=72),
        ],
    },
    {
        "key": "patent_litigation", "name": "Patent / ANDA Litigation (Para IV)",
        "description": "Hatch-Waxman: the 45-day statutory window to sue after a Para IV notice makes early stages hard-SLA'd. Settlements require antitrust review before GC sign-off.",
        "steps": [
            _h(1, "Matter Intake & Docketing", "litigation_intake", "ip_paralegal", sla=24),
            _a(2, "AI Case Summary & Deadline Extraction", "agent_review", "ip_counsel", "litigation_summarizer", 0.7, sla=8),
            _h(3, "IP Counsel Assessment", "ip_assessment", "ip_counsel", sla=120),
            _h(4, "Outside Counsel Engagement", "counsel_engagement", "ip_counsel", sla=168,
               skip_if={"field": "handled_inhouse", "op": "eq", "value": True}),
            _h(5, "Strategy & Budget Approval", "gc_approval", "general_counsel", sla=120),
            _h(6, "Settlement Antitrust Review", "antitrust_review", "competition_counsel", sla=168,
               skip_if={"field": "settlement_proposed", "op": "eq", "value": False}),
            _h(7, "GC / Board Sign-off", "board_signoff", "general_counsel", sla=120),
        ],
    },
    {
        "key": "legal_notice", "name": "Legal Notice Response",
        "description": "Statutory / demand notices with hard reply deadlines. Agent extracts the deadline and drafts; counsel finalizes.",
        "steps": [
            _h(1, "Notice Logging", "notice_intake", "legal_ops", sla=8),
            _a(2, "AI Deadline & Claim Extraction", "agent_review", "legal_team", "notice_analyzer", 0.75, sla=4),
            _h(3, "Response Drafting", "response_draft", "legal_team", sla=72),
            _h(4, "GC Approval & Dispatch", "gc_approval", "general_counsel", sla=48),
        ],
    },
    {
        "key": "regulatory_response", "name": "Regulatory Action Response",
        "description": "USFDA 483 / warning letters, NPPA-DPCO pricing notices, state drug-controller actions. Cross-functional with Quality/Regulatory Affairs.",
        "steps": [
            _h(1, "Action Logging & Classification", "regulatory_intake", "regulatory_affairs", sla=8),
            _h(2, "Cross-functional Assessment", "cfa_review", "quality_head", sla=72),
            _h(3, "Legal Position & Draft Response", "legal_review", "regulatory_counsel", sla=120),
            _h(4, "GC Approval", "gc_approval", "general_counsel", sla=48),
            _h(5, "Board / Disclosure Review", "board_signoff", "company_secretary", sla=48,
               skip_if={"field": "material", "op": "eq", "value": False}),
        ],
    },
    {
        "key": "vendor_onboarding", "name": "Vendor / Counterparty Due Diligence",
        "description": "Third-party onboarding: agent runs sanctions, debarment and adverse-media screening; compliance clears exceptions.",
        "steps": [
            _h(1, "Vendor Details & Documents", "vendor_intake", "procurement"),
            _a(2, "AI Sanctions & Debarment Screening", "agent_review", "compliance_team", "counterparty_screener", 0.85, sla=8),
            _h(3, "Compliance Clearance", "compliance_review", "compliance_team", sla=72),
            _h(4, "Contract Terms Approval", "legal_review", "legal_team", sla=72),
        ],
    },
    {
        "key": "compliance_investigation", "name": "Compliance Investigation",
        "description": "Whistleblower / UCPMP / anti-bribery matters. Confidential track with mandatory closure report.",
        "steps": [
            _h(1, "Complaint Triage", "investigation_intake", "compliance_head", sla=48),
            _h(2, "Investigation Plan Approval", "investigation_plan", "general_counsel", sla=72),
            _h(3, "Fact-finding & Interviews", "investigation_work", "investigation_team", sla=336),
            _h(4, "Findings & Recommendation", "findings_review", "compliance_head", sla=120),
            _h(5, "GC / Audit Committee Closure", "board_signoff", "general_counsel", sla=120),
        ],
    },
    {
        "key": "data_breach", "name": "Data Privacy Incident (DPDP)",
        "description": "Personal-data breach response. The 72-hour notification clock makes the first stages the tightest SLAs in the library.",
        "steps": [
            _h(1, "Incident Logging", "breach_intake", "privacy_officer", sla=4),
            _a(2, "AI Severity & Notification Assessment", "agent_review", "privacy_officer", "breach_assessor", 0.85, sla=4),
            _h(3, "Containment & Legal Position", "legal_review", "privacy_counsel", sla=24),
            _h(4, "Regulator / Data-Principal Notification", "notification_dispatch", "general_counsel", sla=36),
        ],
    },
    {
        "key": "employment_matter", "name": "Employment / POSH Matter",
        "description": "Disciplinary, separation and POSH-committee matters with statutory timelines.",
        "steps": [
            _h(1, "Matter Intake", "hr_intake", "hr_business_partner", sla=48),
            _h(2, "Legal Assessment", "legal_review", "employment_counsel", sla=96),
            _h(3, "Committee / HR Head Decision", "committee_review", "hr_head", sla=168),
            _h(4, "GC Sign-off", "gc_approval", "general_counsel", sla=72),
        ],
    },
    {
        "key": "board_approval", "name": "Board / Secretarial Approval",
        "description": "POAs, authorised-signatory changes, disclosures and resolutions.",
        "steps": [
            _h(1, "Request & Draft Resolution", "secretarial_intake", "company_secretary", sla=72),
            _h(2, "Legal Vetting", "legal_review", "legal_team", sla=72),
            _h(3, "CS / Board Approval", "board_signoff", "company_secretary", sla=168),
        ],
    },
    {
        "key": "trademark_clearance", "name": "Trademark Clearance",
        "description": "AI preliminary clearance on proposed marks — distinctiveness, classes, portfolio conflicts. ALWAYS routes to the IP lead plus a formal registry search; the memo is a first pass, never a clearance.",
        "steps": [
            _h(1, "Mark Details", "trademark_intake", "requester"),
            _a(2, "AI Preliminary Clearance", "agent_review", "ip_counsel", "trademark_clearance_reviewer", 0.99, sla=24),
            _h(3, "IP Lead Review & Formal Search Order", "ip_assessment", "ip_counsel", sla=72),
            _h(4, "Clearance Decision", "gc_approval", "ip_counsel", sla=72),
        ],
    },
    {
        "key": "marketing_review", "name": "Marketing Material Review",
        "description": "Promotional content vs the approved-claims library. Regulated product/therapeutic claims and any new claim ALWAYS require human (and where configured, medical/regulatory-affairs) review — the agent never clears a product claim on its own.",
        "steps": [
            _h(1, "Submit Material", "marketing_intake", "marketing"),
            _a(2, "AI Claim Review", "agent_review", "regulatory_counsel", "marketing_reviewer", 0.9, sla=24),
            _h(3, "Legal / Reg-Affairs Review", "legal_review", "regulatory_counsel", sla=72),
            _h(4, "Approval to Publish", "gc_approval", "general_counsel", sla=48),
        ],
    },
    {
        "key": "contract_type_specialist", "name": "Contract (Type-Specialist)",
        "description": "Commercial contracts reviewed against a per-type playbook (vendor / services / licensing / clinical / ...). The router selects the playbook from the contract type; unmatched types fall through to the generalist Contract Approval ladder.",
        "steps": [
            _h(1, "Draft & Submit", "contract_draft", "contract_owner"),
            _a(2, "AI Type-Specific Review", "agent_review", "legal_team", "contract_type_specialist", 0.85, sla=8),
            _h(3, "Legal Review", "legal_review", "legal_team", sla=48),
            _h(4, "GC Approval", "gc_approval", "general_counsel", sla=72),
            _h(5, "Counter-signature", "signature_screen", "signatory", sla=72),
        ],
    },
]

# ---------------------------------------------------------------------------
# Intake routing: explicit request types, plus a keyword triage fallback for
# free-text requests. Routing stays deterministic — same input, same lane.
# ---------------------------------------------------------------------------

REQUEST_TYPES: dict[str, dict] = {
    "nda":            {"definition_key": "nda_fasttrack",          "label": "NDA / Confidentiality"},
    "contract":       {"definition_key": "clm_contract_approval",  "label": "Commercial Contract"},
    "litigation":     {"definition_key": "patent_litigation",      "label": "Litigation / IP Dispute"},
    "notice":         {"definition_key": "legal_notice",           "label": "Legal Notice"},
    "regulatory":     {"definition_key": "regulatory_response",    "label": "Regulatory Action"},
    "vendor":         {"definition_key": "vendor_onboarding",      "label": "Vendor Due Diligence"},
    "investigation":  {"definition_key": "compliance_investigation", "label": "Compliance Investigation"},
    "data_breach":    {"definition_key": "data_breach",            "label": "Data Privacy Incident"},
    "employment":     {"definition_key": "employment_matter",      "label": "Employment / POSH"},
    "secretarial":    {"definition_key": "board_approval",         "label": "Board / Secretarial"},
    "trademark":      {"definition_key": "trademark_clearance",   "label": "Trademark Clearance"},
    "marketing":      {"definition_key": "marketing_review",      "label": "Marketing Review"},
}

_TRIAGE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("data_breach",   ("breach", "personal data", "dpdp", "data leak", "privacy incident")),
    ("litigation",    ("para iv", "paragraph iv", "anda", "patent", "infringement", "summons",
                       "lawsuit", "plaintiff", "court", "writ", "arbitration")),
    ("regulatory",    ("usfda", "fda", "form 483", "warning letter", "nppa", "dpco",
                       "drug controller", "drug authority", "cdsco", "inspection", "regulator")),
    ("notice",        ("legal notice", "show cause", "demand notice", "notice received", "cease and desist")),
    ("investigation", ("whistleblower", "bribery", "ucpmp", "kickback", "misconduct", "fraud")),
    ("employment",    ("posh", "harassment", "termination", "disciplinary", "employee")),
    ("vendor",        ("vendor", "supplier", "counterparty", "onboarding", "due diligence", "distributor")),
    ("marketing",     ("marketing", "advertis", "promotional", "campaign", "brochure", "landing page")),
    ("trademark",     ("trademark", "brand name", "logo clearance", "word mark", "nice class")),
    ("nda",           ("nda", "non-disclosure", "confidentiality agreement", "cda")),
    ("secretarial",   ("power of attorney", "poa", "board resolution", "authorised signatory", "disclosure")),
    ("contract",      ("contract", "agreement", "msa", "sow", "amendment", "renewal", "license")),
]


def classify(description: str) -> tuple[str, float]:
    """Deterministic triage: first keyword family that matches wins.

    Mechanical and auditable — the same input always routes to the same
    lane. The return contract is ``(request_type, confidence)``.
    """
    text = (description or "").lower()
    for request_type, words in _TRIAGE_KEYWORDS:
        hits = sum(1 for w in words if w in text)
        if hits:
            return request_type, min(0.95, 0.6 + 0.15 * hits)
    return "contract", 0.4          # default lane, low confidence -> human confirms


# ---------------------------------------------------------------------------
# Library agent handlers — deterministic defaults so every ladder runs
# offline. Each handler is pure over the instance's JSONB context and only
# RECOMMENDS: the engine persists the output as a PENDING AgentDecision and a
# human approval executes it. ``proposed_action`` carries no auto-send /
# auto-apply semantics.
# ---------------------------------------------------------------------------

# nda_reviewer is registered by the Contracts module (modules/contracts/
# agents/nda.py) — the ontology-aware v2 replaced the deterministic stub
# that shipped here in the initial port.




# contract_risk_reviewer is registered by the Contracts module
# (modules/contracts/agents/review.py) — ontology-aware v2.




# notice_analyzer is registered by the Regulatory module
# (modules/regulatory/agents/notice.py) — deadline-extracting v2.


# litigation_summarizer is registered by the Matter module
# (modules/matter/agents/litigation.py) — the GraphRAG-cited case-brief v2
# replaced the deterministic stub that shipped here in the initial port.


# counterparty_screener is registered by the Spend & Counsel module
# (modules/spend/agents/vendor.py) — sanctions-list-backed v2.




# breach_assessor is registered by the Privacy Ops module
# (modules/privacy/agents/dpia.py) — the DPIA-scoring v2.
