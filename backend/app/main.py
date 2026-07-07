"""FastAPI application entrypoint.

Composition root for the backend: mounts the module routers, restricts CORS
to the configured frontend origin(s), and exposes the OpenAPI schema at
``/docs`` (the Pydantic schemas are the API contract the frontend's typed
client is generated from).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.modules.audit.router import router as audit_router
from app.modules.brain.router import router as brain_router
from app.modules.cockpit.router import router as cockpit_router
from app.modules.intake.router import router as intake_router
from app.workflow.router import router as workflow_router

# Importing the library registers the deterministic workflow-agent handlers;
# importing the engine registers the workflow.apply_agent_step governed action.
import app.workflow.library  # noqa: F401

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aegis")


@asynccontextmanager
async def lifespan(_: FastAPI):
    mode = "Auth0" if settings.auth0_configured else "DEV (seeded-admin fallback)"
    ai = "configured" if settings.anthropic_api_key else "degraded (no key)"
    logger.info(
        "AEGIS backend starting — env=%s auth=%s ai=%s", settings.environment, mode, ai
    )
    yield


app = FastAPI(
    title="AEGIS Backend",
    version="0.1.0",
    description=(
        "AEGIS legal operations platform — FastAPI backend. Owns all business "
        "logic, data access, AI agents, the cryptographic audit chain, and RBAC. "
        "The Next.js frontend is a presentation + thin BFF layer."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_V1 = "/api/v1"
app.include_router(intake_router, prefix=API_V1)
app.include_router(audit_router, prefix=API_V1)
app.include_router(cockpit_router, prefix=API_V1)
app.include_router(workflow_router, prefix=API_V1)
app.include_router(brain_router, prefix=API_V1)


@app.get("/health", tags=["health"], summary="Liveness probe.")
async def health() -> dict:
    return {
        "status": "ok",
        "environment": settings.environment,
        "auth0_configured": settings.auth0_configured,
        "ai_configured": bool(settings.anthropic_api_key),
    }
