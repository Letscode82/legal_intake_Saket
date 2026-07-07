# AEGIS Backend Roadmap — Ontology Brain, Workflow Engine, Specialist Agents

> Working plan for the Next.js → FastAPI split, reconciled across three
> sources: the core migration brief (the non-negotiables), the
> `GC Suite Agents — Working Architecture` document (11 specialists,
> ontology + GraphRAG), and the uploaded dynamic workflow engine
> (governance ladders / Legal Front Door). PR 1 (FastAPI foundation +
> human-gated NDA path) is merged; everything below builds on it.

## Locked decisions

| Decision | Choice | Rationale |
|---|---|---|
| Agent count | **6 committed now** (NDA, Contract Review, Vendor/Sanctions, Policy Q&A, FAQ, Trademark); the doc's other 5 (Notice, Privacy DPIA, Marketing Review, Litigation Support, Contract-Type Specialist) staged as opt-in **Phase B2** | Core brief: "~6 agents… never agent-ify a mechanical step" |
| Graph engine | **Recursive CTEs + pgvector in plain Postgres** (no Apache AGE) | "Simplest solution", Neon-native, honors "no raw SQL outside db/" |
| Embeddings | **Provider abstraction (Voyage AI)** behind one interface; **degrades to Postgres full-text search** when no key | Claude does not embed; degrade pattern mirrors the AI-client degrade |
| Agents ↔ modules | Agents map INTO the 11 locked modules (Trademark → Matter; Notice/Marketing → Regulatory; FAQ → Knowledge; Policy → Governance; DPIA → Privacy Ops; Vendor → Spend; contracts agents → Contracts; Cockpit/Ask-the-Brain → Command Center) | Module list is LOCKED — agents never imply a 12th module |
| Workflow engine | Ported as the shared `workflow` package (not a module); the intake Front Door routes through it; agent steps host the specialists | Fills the `@aegis/workflow` stub; one router for everything reaching Legal |

## Non-negotiables enforced at every PR

1. **Human gate**: agents only ever produce a PENDING `AgentDecision`; the
   only path to APPROVED is a human call that executes the governed action
   and writes the chain-sealed audit row in the same transaction. The
   workflow engine's auto-apply of high-confidence agent decisions is
   REMOVED on port — high confidence only pre-fills the Cockpit.
2. **Deterministic spine**: classification, routing, SLA math, dedup,
   ticket creation, notifications are plain Python. One classifier
   (`classify(description) → (request_type, confidence)`) feeds both
   intake and the workflow router.
3. **One AI module** (`core/ai.py`) — every call persisted to
   `ai_call_log`. **One db layer** — raw SQL only in Alembic migrations
   (the GraphRAG recursive CTEs live inside the db package).
4. **Shared entities once**; ontology edges are typed links between them,
   never duplicate entities.
5. **Audit chain**: every workflow transition twin-records to the
   hash-chained `audit_log`; defensibility export ships in PR 2.
6. **Security**: untrusted-input spotlighting for all document/email
   content fed to models (OWASP LLM-01); RBAC (`require_permission`) on
   every mutation; CORS restricted to the frontend origin. Every agent PR
   ships prompt-injection fixtures asserting the gate holds.
7. **Demo stays green**: the existing Next.js/Prisma demo runs untouched
   until the PR 20 cutover.

## PR plan

### Phase A — the central brain + workflow spine

| PR | Title | Scope | Depends |
|---|---|---|---|
| 1 ✅ | FastAPI foundation + NDA path | Merged: db + audit chain + auth/RBAC + ai client + human-gated intake NDA flow, 15 tests | — |
| 2 | Ontology core + generalized AgentDecision gate + defensibility export | `ontology_edge` (typed links), `agent_decision` (PENDING→APPROVED/APPROVED_WITH_OVERRIDE/REJECTED) + governed-action registry (approve executes + audits in one txn), `ai_call_log`, `GET /audit/export` (canonical content per row, off-database verifiable) | 1 |
| 3 | Workflow engine → shared `workflow` package | Port async + org-scoped + cuid ids; definitions (versioned; instances pin a version), steps ≤15, instances, transitions, notifications outbox, `AgentTask` queue; approve/reject/send_back/cancel; `skip_if` rules, SLA→RAG aging, optimistic lock; **agent steps emit PENDING AgentDecision — approval advances the ladder**; every transition twin-records to `audit_log`; RBAC on all actions; admin builder API | 2 |
| 4 | GraphRAG retrieval service | Recursive-CTE k-hop over `ontology_edge` + pgvector + Postgres FTS (BM25-style) + rank fusion; permission-filtered traversal; cited context + explicit gap note; embedding-provider abstraction (Voyage; FTS-only degrade) | 2 |
| 5 | Cockpit + requester status | Generic approve/edit/reject over AgentDecision (reasoning, confidence, concerns, clickable citations); ports `WorkflowWizard`/`RagProgress` UI; requester live status/SLA | 2,3 |
| 6 | Intake Front Door / smart routing | One door: explicit `request_type` or `classify()` triage → start the matter-type ladder; matter numbering; `IntakeTicket ↔ WorkflowInstance` link; **dedup** (`external_message_id`) + idempotency keys | 3 |
| 7 | Documents & untrusted-input pipeline | Upload on shared `Document` (type/size limits), text extraction, untrusted-content spotlighting for model calls | 2 |
| 8 | "Ask the Brain" NL query | Read-only NL endpoint over GraphRAG — permission-filtered, cited, gap-noted | 4 |

### Phase B — the 6 committed agents (each: GraphRAG reads → PENDING AgentDecision → ontology writes; golden-set evals + injection fixtures in DoD)

| PR | Agent | Home module | Key reads | Key writes | External |
|---|---|---|---|---|---|
| 9 | NDA v2 (+ shared eval runner) | Contracts | 2-hop Counterparty subgraph → prior `Document(NDA)` via `PARTY_TO`; sanctions flag | `Document(NDA)`, `NDA_WITH` edge (term/expiry) | — |
| 10 | Vendor / Sanctions Screening | Legal Spend & Counsel | Counterparty `OWNED_BY`/`RELATED_TO`, prior `ScreeningResult` | `ScreeningResult` + `SCREENED_ON`; sanctions flag propagates | OFAC SDN |
| 11 | Contract Review (generalist) | Contracts | Counterparty contract family; open `Obligation` conflicts; precedent vectors | `Contract` + `OBLIGATES` into obligation register | — |
| 12 | FAQ | Knowledge | Versioned `KnowledgeEntry` (hybrid search) | `AnswerRecord` + `CITES` (read-only answer; low confidence hands off) | — |
| 13 | Policy Q&A | Governance | Versioned `Policy` corpus only | `AnswerRecord` + `CITES`; `PolicyConflict` routed to owner | — |
| 14 | Trademark Clearance | Matter Mgmt | `Mark` portfolio, prior `ClearanceMemo` | `ClearanceMemo`, `Mark`; approval spawns Matter `CONCERNS`→Mark | USPTO/EUIPO path |

### Phase B2 — opt-in specialists (approve before build; ladders already exist in the engine library)

| PR | Agent | Home module | Rides existing ladder |
|---|---|---|---|
| 15 | Notice Management | Regulatory | `legal_notice` |
| 16 | Data-Privacy Assessment | Privacy Ops | `data_breach` |
| 17 | Marketing-Material Review | Regulatory | `regulatory_response` |
| 18 | Litigation Support | Matter / Legal Hold | `patent_litigation` |
| 19 | Contract-Type Specialist | Contracts | `clm_contract_approval` |

### Phase C — hardening & cutover

| PR | Title | Scope | Depends |
|---|---|---|---|
| 20 | Indexer + worker + overnight jobs | Event-driven embedding/edge refresh; engine worker loop (background AgentTask execution); entity enrichment + citation repair | 3,4 |
| 21 | Full Next.js rewiring | openapi-typescript client + TanStack Query; replace `pages/api/*` business routes; Cockpit/intake/insights/admin pages on the backend; per-permission UI gating | 5,6,8 |
| 22 | Deploy + observability + Auth0 cutover | Container on Fly/Render/Railway; Neon; CORS to Vercel origin; structured logs + per-agent/per-org token metrics; rate limiting on intake; production Auth0 PKCE flow | 21 |

### Named non-goals (post-v1, deliberate)

- Word-native `.docx` redline output for contract agents.
- Multi-regime sanctions feeds (EU/UN/UK) beyond OFAC.
- Conversational intake channel (form/email/upload first).
- Apache AGE / openCypher — revisit only if CTE traversal hits limits.
