# AEGIS Backend (FastAPI)

The FastAPI service that owns **all** business logic, data access, the AI
agents, the cryptographic audit chain, and RBAC for AEGIS. The Next.js app is
a presentation + thin BFF layer that calls this backend over HTTP with an
Auth0 Bearer token — it holds no business logic, no database access, and no
Anthropic key.

This is the **first increment** of the monorepo split: the backend foundation
(db + audit chain + auth + AI client + permissions) plus the **Legal Intake
NDA path, fully human-gated**, per the migration plan ("stand up the backend
with the db + audit chain first; migrate one module end-to-end first"). The
remaining intake surfaces (Kanban, SLA, smart routing, copilot) and the other
modules expand from this spine.

## Architecture map

```
app/
  main.py                  FastAPI composition root (CORS, routers, /docs)
  core/
    config.py              Settings; fails loud in production on missing vars
    permissions.py         Permission enum + role bundles + can_user_do (RBAC)
    security.py            Auth0 JWT (JWKS) validation → get_current_actor,
                           require_permission; dev-mode seeded-admin fallback
    audit.py               log_audit() + verify_audit_chain()
    governance.py          AgentDecision gate: PENDING → human approve
                           executes the governed action + audit, one txn
    ai.py                  call_claude / call_claude_json — the ONE key site;
                           every call persisted to ai_call_log
    ids.py                 opaque id generation
  db/
    models.py              Shared entities (Org, Role, User, Person,
                           Counterparty, Document, Obligation, Event, Tag,
                           Tagging, AuditLog) + ontology_edge, agent_decision,
                           ai_call_log + intake tables — defined ONCE
    ontology.py            typed-link authoring (add_edge, get_neighbors)
    session.py             async engine + session dependency
    seed.py                idempotent demo seed (org, 8 roles, admin, users)
    migrations/            Alembic; 0002 installs the audit-chain triggers
  modules/
    intake/                agents/ (base, classifier, nda, router),
                           schemas.py, service.py (logic + audit), router.py
    audit/                 router.py (verify + list ledger)
tests/                     pytest + httpx AsyncClient + db-integrity
```

## The non-negotiables, in code

- **Conservative AI governance.** An agent only ever produces a
  `Recommendation`, persisted `PENDING`. The *only* path to `APPROVED` is
  `POST /intake/tickets/{id}/approve` — a human call that also writes the
  audit row in the same transaction (`app/modules/intake/service.py`).
- **Deterministic spine.** Classification / priority / SLA are pure Python
  (`agents/classifier.py`), never an LLM. Agents exist only where reasoning
  over text is required (NDA here; contract/vendor/policy/FAQ/trademark next).
- **All AI through one module.** `core/ai.py` is the only place the Anthropic
  key is read. No key → agents fall back to a degraded, human-review-only
  recommendation (confidence 0.4, `flag-for-review`, never auto-send).
- **All data through one layer.** `db/` owns the engine + models + `log_audit`.
  Raw SQL lives only in Alembic migrations.
- **Shared entities once.** No `MatterCounterparty`, `IntakeDocument`, etc.
- **Cryptographic audit chain.** `audit_log` is append-only (Postgres triggers
  block UPDATE/DELETE) and hash-chained per org. `verify_audit_chain` detects
  tampering even if an attacker disables the triggers, because it recomputes
  each row's hash from stored fields (ported from the frontend's D11 design).
- **RBAC in the backend.** Every mutation goes through `require_permission`.

## Run it locally

```bash
# 1. Postgres (repo root)
docker compose up -d

# 2. Backend
cd backend
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env                 # defaults work against the compose DB

alembic upgrade head                 # apply migrations (incl. audit chain)
python -m app.db.seed                # seed org + roles + users
uvicorn app.main:app --reload --port 8000
```

- OpenAPI docs: <http://localhost:8000/docs>
- Health: <http://localhost:8000/health>

With Auth0 unconfigured, dev mode resolves every request to the seeded admin
(override with the `X-Dev-User-Email` header, e.g. `requester@aegis-demo.example`,
to preview a role). Production **requires** the Auth0 vars — the config guard
crashes startup otherwise.

## Tests

```bash
pytest
```

`tests/conftest.py` provisions a fresh `aegis_test` database, applies **all**
migrations from scratch via the real `alembic upgrade head`, seeds it, and
runs against the ASGI app. Coverage:

- `test_db_integrity.py` — migrations-from-scratch + seed + chain verifies;
  UPDATE/DELETE on `audit_log` are blocked.
- `test_audit_chain.py` — tamper detection survives triggers being disabled.
- `test_intake_flow.py` — classify → PENDING recommendation → human approval
  gate → audited + chain intact; RBAC (a requester cannot approve).
- `test_permissions.py` — role-bundle invariants.
- `test_governance.py` — the AgentDecision gate: PENDING-only agent writes,
  approve executes action + audit atomically (executor failure leaves
  PENDING), exactly-once decisions, idempotent ontology edges.
- `test_defensibility_export.py` — an auditor with no DB access re-verifies
  the exported chain by SHA-256-ing each row's verbatim canonical content.

## Frontend integration (next increment)

The Pydantic schemas are the API contract. Generate a typed client from the
OpenAPI schema and fetch with TanStack Query:

```bash
npx openapi-typescript http://localhost:8000/openapi.json -o apps/web/lib/api-types.ts
```

Set `NEXT_PUBLIC_BACKEND_URL` on the frontend; the Next.js app forwards the
Auth0 access token as `Authorization: Bearer <jwt>` on every backend call. The
old `apps/web/pages/api/*` business routes are replaced by calls to this
backend (either directly from client components, or via slim BFF route
handlers that forward the request + token — never business logic).

## Deploy

- **Backend:** container (`Dockerfile`) on Fly.io / Render / Railway; Postgres
  on Neon. Set `DATABASE_URL`, `ANTHROPIC_API_KEY`, `AUTH0_DOMAIN`,
  `AUTH0_AUDIENCE`, `ENCRYPTION_KEY`, and CORS `FRONTEND_ORIGINS` to the
  Vercel origin.
- **Frontend:** Next.js on Vercel, with `NEXT_PUBLIC_BACKEND_URL`.
