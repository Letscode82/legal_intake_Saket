"""Idempotent demo seed.

Seeds the demo organization, the 8 canonical roles with their permission
bundles, the admin User (+ linked Person), and a demo requester Person. Run
with ``python -m app.db.seed``. Production must set ``SEED_ADMIN_EMAIL`` and
``SEED_ADMIN_NAME`` — the fallback email is a non-routable demo domain and
exists for local dev / CI only.
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import select

from app.core.permissions import ALL_ROLES, ROLE_PERMISSIONS
from app.db.models import Organization, Person, Role, User
from app.db.session import get_sessionmaker

DEMO_ORG_NAME = "AEGIS Demo GC"
LEGACY_ADMIN_NAME = "Alex Nguyen"


async def seed() -> None:
    admin_email = os.environ.get("SEED_ADMIN_EMAIL", "alex.nguyen@aegis-demo.example")
    admin_name = os.environ.get("SEED_ADMIN_NAME", LEGACY_ADMIN_NAME)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # ── Organization ────────────────────────────────────────────
        org = (
            await session.execute(
                select(Organization).where(Organization.name == DEMO_ORG_NAME)
            )
        ).scalars().first()
        if org is None:
            org = Organization(name=DEMO_ORG_NAME, tier="DEMO", region="US")
            session.add(org)
            await session.flush()

        # ── Roles (all 8, with permission bundles) ──────────────────
        roles_by_name: dict[str, Role] = {}
        for role_name in ALL_ROLES:
            role = (
                await session.execute(
                    select(Role).where(
                        Role.organization_id == org.id, Role.name == role_name
                    )
                )
            ).scalars().first()
            perms = [p.value for p in ROLE_PERMISSIONS[role_name]]
            if role is None:
                role = Role(organization_id=org.id, name=role_name, permissions=perms)
                session.add(role)
                await session.flush()
            else:
                role.permissions = perms
            roles_by_name[role_name] = role

        # ── Admin User (idempotent across email/name changes) ───────
        admin = (
            await session.execute(
                select(User).where(
                    User.organization_id == org.id,
                    (User.name == admin_name)
                    | (User.name == LEGACY_ADMIN_NAME)
                    | (User.email == admin_email),
                )
            )
        ).scalars().first()
        if admin is None:
            admin = User(
                organization_id=org.id,
                email=admin_email,
                name=admin_name,
                role_id=roles_by_name["admin"].id,
            )
            session.add(admin)
            await session.flush()
        else:
            admin.email = admin_email
            admin.name = admin_name
            admin.role_id = roles_by_name["admin"].id

        # Person linked to the admin (so "own tickets" resolves).
        admin_person = (
            await session.execute(
                select(Person).where(
                    Person.organization_id == org.id, Person.user_id == admin.id
                )
            )
        ).scalars().first()
        if admin_person is None:
            session.add(
                Person(
                    organization_id=org.id,
                    type="EMPLOYEE",
                    user_id=admin.id,
                    name=admin_name,
                    email=admin_email,
                )
            )
        else:
            admin_person.name = admin_name
            admin_person.email = admin_email

        # ── Per-role preview users (one per non-admin role) ─────────
        # Mirrors the frontend seed §7 so the demo can be previewed through
        # each role lens (dev-mode: set X-Dev-User-Email / DEV_USER_EMAIL).
        for role_name in ALL_ROLES:
            if role_name == "admin":
                continue
            email = f"{role_name}@aegis-demo.example"
            existing = (
                await session.execute(
                    select(User).where(
                        User.organization_id == org.id, User.email == email
                    )
                )
            ).scalars().first()
            if existing is None:
                u = User(
                    organization_id=org.id,
                    email=email,
                    name=f"{role_name.replace('_', ' ').title()} User",
                    role_id=roles_by_name[role_name].id,
                )
                session.add(u)
                await session.flush()
                session.add(
                    Person(
                        organization_id=org.id,
                        type="EMPLOYEE",
                        user_id=u.id,
                        name=u.name,
                        email=email,
                    )
                )
            else:
                existing.role_id = roles_by_name[role_name].id

        # ── Demo requester Person ───────────────────────────────────
        requester = (
            await session.execute(
                select(Person).where(
                    Person.organization_id == org.id,
                    Person.email == "dana.li@aegis-demo.example",
                )
            )
        ).scalars().first()
        if requester is None:
            session.add(
                Person(
                    organization_id=org.id,
                    type="EMPLOYEE",
                    name="Dana Li",
                    email="dana.li@aegis-demo.example",
                    payload_metadata={"department": "Sales"},
                )
            )

        await session.commit()
        print(f"Seeded org={org.id} admin={admin.email} roles={len(roles_by_name)}")


if __name__ == "__main__":
    asyncio.run(seed())
