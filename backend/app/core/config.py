"""Application configuration.

Single source of truth for environment-derived settings. Fails loud in
production when a required variable is missing — mirrors the Next.js
`@aegis/auth` production guard (AUTH0_SECRET unset → throw at import). A
misconfigured production deploy must crash at startup with a clear error,
never silently downgrade to a permissive dev fallback.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Environment ──────────────────────────────────────────────────
    environment: str = Field(default="development", alias="ENVIRONMENT")

    # ── Database ─────────────────────────────────────────────────────
    # Async SQLAlchemy URL (postgresql+asyncpg://...). Local dev points at
    # the docker-compose Postgres; production at Neon.
    database_url: str = Field(
        default="postgresql+asyncpg://aegis:aegis@localhost:5432/aegis",
        alias="DATABASE_URL",
    )

    # ── AI (Anthropic) ───────────────────────────────────────────────
    # The ONLY place the Anthropic key is ever read. When unset the agents
    # fall back to a degraded, human-review-only recommendation — the demo
    # still walks end-to-end without a key.
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-sonnet-4-5", alias="ANTHROPIC_MODEL")

    # ── Embeddings (GraphRAG vector leg) ─────────────────────────────
    # Claude does not embed; Voyage AI is the Anthropic-recommended pairing
    # (voyage-law-2 is legal-domain-tuned). Unset → retrieval degrades to
    # Postgres full-text search only, same pattern as the AI degrade.
    voyage_api_key: str | None = Field(default=None, alias="VOYAGE_API_KEY")
    voyage_model: str = Field(default="voyage-law-2", alias="VOYAGE_MODEL")

    # ── Auth0 ────────────────────────────────────────────────────────
    # The frontend runs the Auth0 login and forwards the access token; the
    # backend validates the JWT (issuer, audience, signature via JWKS).
    auth0_domain: str | None = Field(default=None, alias="AUTH0_DOMAIN")
    auth0_audience: str | None = Field(default=None, alias="AUTH0_AUDIENCE")

    # Dev-mode fallback: with Auth0 unconfigured, every request resolves to
    # the seeded admin (overridable). Matches the Next.js zero-config dev
    # experience. HARD-disabled in production by the guard below.
    dev_user_email: str = Field(
        default="alex.nguyen@aegis-demo.example", alias="DEV_USER_EMAIL"
    )

    # ── CORS ─────────────────────────────────────────────────────────
    # Comma-separated list of allowed frontend origins.
    frontend_origins: str = Field(
        default="http://localhost:3000,http://localhost:5173",
        alias="FRONTEND_ORIGINS",
    )

    # ── At-rest encryption ───────────────────────────────────────────
    # Used for any secret persisted at rest (e.g. per-org integration
    # credentials). Required in production.
    encryption_key: str | None = Field(default=None, alias="ENCRYPTION_KEY")

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    @property
    def auth0_configured(self) -> bool:
        return bool(self.auth0_domain and self.auth0_audience)

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.frontend_origins.split(",") if o.strip()]

    @model_validator(mode="after")
    def _fail_loud_in_production(self) -> "Settings":
        """Crash at startup if a production deploy is misconfigured.

        The whole point of this platform is a defensible audit trail and a
        real authenticated actor on every mutation. A production instance
        without Auth0 would resolve every visitor to the seeded admin — that
        footgun must never ship. Same discipline as the frontend's
        module-load guard.
        """
        if not self.is_production:
            return self

        missing: list[str] = []
        if not self.auth0_domain:
            missing.append("AUTH0_DOMAIN")
        if not self.auth0_audience:
            missing.append("AUTH0_AUDIENCE")
        if not self.anthropic_api_key:
            # AI can degrade, but a production instance should have a key.
            missing.append("ANTHROPIC_API_KEY")
        if not self.encryption_key:
            missing.append("ENCRYPTION_KEY")
        if missing:
            raise RuntimeError(
                "AEGIS backend refusing to start in production: missing required "
                f"environment variables {missing}. Set them on the deployment "
                "target. (Dev-mode fallbacks are disabled in production by design.)"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
