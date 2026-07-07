"""Authentication + authorization dependencies.

The Next.js frontend runs the Auth0 login (Authorization Code + PKCE) and
forwards the resulting access token as ``Authorization: Bearer <jwt>``. This
backend validates the JWT (issuer, audience, signature via the Auth0 JWKS)
and resolves the platform ``User``/``Organization`` rows — that resolved
actor is dependency-injected into every handler.

Dev-mode fallback: when Auth0 is unconfigured, every request resolves to the
seeded admin (overridable via ``DEV_USER_EMAIL`` or the ``X-Dev-User-Email``
header) so ``uvicorn`` runs zero-config locally. This fallback is
HARD-disabled in production by the config guard — a production instance
without Auth0 refuses to start.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.permissions import Permission, can_user_do
from app.db.models import Role, User
from app.db.session import get_session


@dataclass(frozen=True)
class Actor:
    """The resolved, authenticated caller for a request."""

    user_id: str
    organization_id: str
    email: str
    name: str
    role_name: str | None
    permissions: frozenset[str]

    def has(self, permission: Permission) -> bool:
        return permission.value in self.permissions


class AccessDeniedError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@lru_cache
def _jwks_client() -> "jwt.PyJWKClient":
    return jwt.PyJWKClient(f"https://{settings.auth0_domain}/.well-known/jwks.json")


def _decode_auth0_token(token: str) -> dict:
    """Validate signature + issuer + audience against Auth0."""
    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.auth0_audience,
            issuer=f"https://{settings.auth0_domain}/",
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid access token: {exc}",
        ) from exc


def _claim_email(claims: dict) -> str | None:
    """Extract the email from Auth0 claims.

    Access tokens may not carry ``email`` unless a custom claim is added via
    an Auth0 Action. We check the common namespaced claim first, then the
    standard one, then fall back to ``sub`` (used only for lookup).
    """
    for key in ("https://aegis.legal/email", "email"):
        if claims.get(key):
            return claims[key]
    return claims.get("sub")


async def _resolve_user(session: AsyncSession, *, email: str) -> User | None:
    result = await session.execute(
        select(User).where(User.email == email, User.suspended_at.is_(None))
    )
    return result.scalars().first()


async def get_current_actor(
    request: Request,
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(default=None),
    x_dev_user_email: str | None = Header(default=None),
) -> Actor:
    if settings.auth0_configured:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing bearer token.",
            )
        token = authorization.split(" ", 1)[1].strip()
        claims = _decode_auth0_token(token)
        email = _claim_email(claims)
        if not email:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has no resolvable identity claim.",
            )
    else:
        # Dev-mode fallback (never reached in production — config guard).
        email = x_dev_user_email or settings.dev_user_email

    user = await _resolve_user(session, email=email)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"No AEGIS user provisioned for {email}.",
        )

    permissions: frozenset[str] = frozenset()
    role_name: str | None = None
    if user.role_id:
        role = await session.get(Role, user.role_id)
        if role:
            role_name = role.name
            permissions = frozenset(role.permissions or [])

    return Actor(
        user_id=user.id,
        organization_id=user.organization_id,
        email=user.email,
        name=user.name,
        role_name=role_name,
        permissions=permissions,
    )


def require_permission(permission: Permission):
    """Dependency factory: 403 unless the actor holds ``permission``.

    This is the authoritative action-grant check. Resource-scope predicates
    (matter assignment, ticket ownership) are enforced additionally inside
    the service layer where the resource is loaded.
    """

    async def _dependency(actor: Actor = Depends(get_current_actor)) -> Actor:
        decision = can_user_do(set(actor.permissions), permission)
        if not decision.allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=decision.message
            )
        return actor

    return _dependency
