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
from app.modules.intake.router import router as intake_router

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


@app.get("/health", tags=["health"], summary="Liveness probe.")
async def health() -> dict:
    return {
        "status": "ok",
        "environment": settings.environment,
        "auth0_configured": settings.auth0_configured,
        "ai_configured": bool(settings.anthropic_api_key),
    }
