# AEGIS — Two-Deployable Split: Deployment & Frontend Integration

AEGIS runs as **two deployables**:

| Deployable | What it is | Hosts |
|---|---|---|
| **Frontend** (`apps/web`) | Next.js 14 — presentation + a thin **BFF proxy**. No business logic, no DB, no Anthropic key. | Vercel (as today) |
| **Backend** (`backend/`) | FastAPI — owns all business logic, data, the AI agents, the audit chain, RBAC. Versioned REST at `/api/v1`, OpenAPI at `/docs`. | Fly.io / Render / Railway |
| **Database** | PostgreSQL (`pgvector`-capable) | Neon (prod) |

## How the frontend talks to the backend

Two seams, both shipped:

1. **BFF proxy** — `apps/web/pages/api/backend/[...path].ts` forwards
   `/api/backend/<x>` → `${BACKEND_URL}/api/v1/<x>`, attaching the Auth0
   access token as a Bearer header. **Forward-only** — no business logic.
   The browser never holds the token or sees the backend URL.
2. **Typed client** — `apps/web/lib/aegis-backend/client.ts` (`backend` +
   `aegis` helpers) calls the proxy. Regenerate end-to-end types from the
   live backend with `pnpm --filter @aegis/web gen:api` (writes
   `lib/aegis-backend/schema.ts`).

Route protection stays in Next.js `middleware.ts`; per-permission UI gating
hides affordances, but the backend enforces authoritatively on every
mutation.

### Migrating a screen (the incremental path)

The old `pages/api/*` business routes still run on the legacy Prisma stack,
so the demo works throughout the cutover. Migrate one screen at a time:

1. Replace its data fetch with `aegis.*` / `backend.*` (via the proxy).
2. Delete the corresponding `pages/api/<x>` business route once no caller
   remains.
3. `/api/claude` is removed on cutover — all AI now runs backend-side.

This is deliberately incremental and each step is independently verifiable
by running both services (below). It is the remaining hands-on work of the
split; the connective tissue (proxy, client, env, containers) is in place.

## Environment

**Frontend (Vercel):**
```
AUTH0_SECRET, AUTH0_BASE_URL, AUTH0_ISSUER_BASE_URL, AUTH0_CLIENT_ID,
AUTH0_CLIENT_SECRET, AUTH0_AUDIENCE          # Auth0 login + token
BACKEND_URL=https://aegis-backend.fly.dev    # server-side (BFF proxy)
NEXT_PUBLIC_BACKEND_URL=https://aegis-backend.fly.dev
```

**Backend (Fly/Render):**
```
ENVIRONMENT=production
DATABASE_URL=postgresql+asyncpg://...neon...
ANTHROPIC_API_KEY=...
AUTH0_DOMAIN=<tenant>.auth0.com   AUTH0_AUDIENCE=<api-identifier>
ENCRYPTION_KEY=...
FRONTEND_ORIGINS=https://<vercel-app>.vercel.app   # CORS allow-list
```
Missing required vars crash startup in production by design
(`app/core/config.py`) — no silent dev-mode fallback ships.

## Run the split locally

```bash
# Backend + Postgres together
docker compose -f docker-compose.split.yml up --build
docker compose -f docker-compose.split.yml exec backend python -m app.db.seed

# Frontend (separate terminal)
cd apps/web
BACKEND_URL=http://localhost:8000 NEXT_PUBLIC_BACKEND_URL=http://localhost:8000 \
  pnpm --filter @aegis/web dev      # http://localhost:5173
```

Backend-only (no Docker): see `backend/README.md`.

## Auth0 cutover

The frontend already runs Authorization Code + PKCE and sets the session.
For the backend to validate tokens, add an **API** in Auth0 (identifier =
`AUTH0_AUDIENCE`) and request that audience on login so the access token is
a JWT the backend's JWKS validation accepts. Until Auth0 is configured on
the backend, dev mode resolves the seeded admin — never enabled in
production (the config guard blocks it).

## Deploy

- **Backend:** `fly deploy` (see `backend/fly.toml`) or the Render blueprint
  (`backend/render.yaml`). `alembic upgrade head` runs as the release step.
- **Frontend:** Vercel, with the env vars above. Set `BACKEND_URL` to the
  deployed backend and `FRONTEND_ORIGINS` on the backend to the Vercel
  origin so CORS admits only the frontend.
