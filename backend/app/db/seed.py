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

        # ── Demo ontology: counterparties + a prior NDA + typed edges ──
        # Gives "Ask the Brain" something real to answer ("do we have an
        # NDA with Acme?") before the agents start authoring edges.
        from app.db.models import Counterparty, Document
        from app.db.ontology import NodeRef, add_edge

        async def _counterparty(name: str, cp_type: str, country: str) -> Counterparty:
            row = (
                await session.execute(
                    select(Counterparty).where(
                        Counterparty.organization_id == org.id,
                        Counterparty.name == name,
                    )
                )
            ).scalars().first()
            if row is None:
                row = Counterparty(
                    organization_id=org.id, name=name, type=cp_type, country=country
                )
                session.add(row)
                await session.flush()
            return row

        acme = await _counterparty("Acme Corporation", "COMPANY", "US")
        await _counterparty("Meridian Biotech", "COMPANY", "CH")

        nda_doc = (
            await session.execute(
                select(Document).where(
                    Document.organization_id == org.id,
                    Document.name == "Mutual NDA — Acme Corporation (executed)",
                )
            )
        ).scalars().first()
        if nda_doc is None:
            nda_doc = Document(
                organization_id=org.id,
                name="Mutual NDA — Acme Corporation (executed)",
                mime_type="application/pdf",
                size_bytes=48_213,
                storage_url="seed://documents/nda-acme-2025.pdf",
                owner_type="CONTRACT",
                owner_id="seed",
                uploaded_by=admin.id,
                extracted_text=(
                    "Mutual Non-Disclosure Agreement between Acme Corporation "
                    "and AEGIS Demo GC. Term: 2 years from the Effective Date "
                    "(2025-06-30). Standard carve-outs; mutual no-solicit 12 "
                    "months; Delaware governing law."
                ),
            )
            session.add(nda_doc)
            await session.flush()

        await add_edge(
            session, organization_id=org.id,
            src=NodeRef("Counterparty", acme.id), label="NDA_WITH",
            dst=NodeRef("Organization", org.id),
            properties={"term_years": 2, "effective": "2025-06-30",
                        "expires": "2027-06-30"},
            source_module="seed", created_by=admin.id,
        )
        await add_edge(
            session, organization_id=org.id,
            src=NodeRef("Document", nda_doc.id), label="PARTY_TO",
            dst=NodeRef("Counterparty", acme.id),
            properties={"document_type": "NDA"},
            source_module="seed", created_by=admin.id,
        )

        # ── Agent-fleet reference data (PRs 10–14) ─────────────────────
        from app.db.models import KnowledgeEntry, Mark, SanctionsEntry

        for entry in [
            {"name": "Blackhat Global Trading FZE",
             "aliases": ["Blackhat Trading", "BGT FZE"],
             "country": "AE", "program": "DEMO-SDN"},
            {"name": "Volkov Industries LLC",
             "aliases": ["Volkov Industrial Group"],
             "country": "RU", "program": "DEMO-SDN"},
            {"name": "Northstar Shipping Co",
             "aliases": [], "country": "IR", "program": "DEMO-SDN"},
        ]:
            existing_row = (
                await session.execute(
                    select(SanctionsEntry).where(
                        SanctionsEntry.list_source == "OFAC_SDN_DEMO",
                        SanctionsEntry.name == entry["name"],
                    )
                )
            ).scalars().first()
            if existing_row is None:
                session.add(SanctionsEntry(list_source="OFAC_SDN_DEMO", **entry))

        kb_rows = [
            {"kind": "FAQ", "slug": "nda-turnaround", "topic": "nda",
             "title": "How long does an NDA take?",
             "body": "Standard mutual NDAs on our template (MNDA-v4.2) are "
                     "typically turned around within 1 business day via the "
                     "Legal Front Door. Non-standard terms route to counsel "
                     "and take 3-5 days."},
            {"kind": "FAQ", "slug": "contract-signature-authority", "topic": "signature",
             "title": "Who can sign contracts?",
             "body": "Only officers listed in the signature authority matrix "
                     "may execute contracts. Contracts above $250k require "
                     "GC counter-signature."},
            {"kind": "POLICY", "slug": "gifts-entertainment", "topic": "gifts",
             "title": "Gifts & Entertainment Policy",
             "body": "Employees may not accept gifts exceeding $150 in value "
                     "from any vendor or counterparty. All gifts from "
                     "government officials must be declined and reported to "
                     "Compliance within 48 hours. (v2, effective 2026-01-01)"},
            {"kind": "POLICY", "slug": "outside-counsel-engagement", "topic": "counsel",
             "title": "Outside Counsel Engagement Policy",
             "body": "Engaging outside counsel requires Legal Ops approval "
                     "and an executed engagement letter with agreed rates. "
                     "Matters above $100k estimated fees require GC approval."},
        ]
        for kb in kb_rows:
            existing_row = (
                await session.execute(
                    select(KnowledgeEntry).where(
                        KnowledgeEntry.organization_id == org.id,
                        KnowledgeEntry.kind == kb["kind"],
                        KnowledgeEntry.slug == kb["slug"],
                    )
                )
            ).scalars().first()
            if existing_row is None:
                session.add(
                    KnowledgeEntry(
                        organization_id=org.id, version=1, is_current=True,
                        owner_user_id=admin.id, **kb,
                    )
                )

        for mark in [
            {"name": "AEGIRA", "nice_classes": [9, 42],
             "jurisdictions": ["US", "EU"], "status": "REGISTERED"},
            {"name": "NOVAPULSE", "nice_classes": [5],
             "jurisdictions": ["US"], "status": "PENDING"},
        ]:
            existing_row = (
                await session.execute(
                    select(Mark).where(
                        Mark.organization_id == org.id, Mark.name == mark["name"]
                    )
                )
            ).scalars().first()
            if existing_row is None:
                session.add(Mark(organization_id=org.id, **mark))

        await session.commit()
        print(f"Seeded org={org.id} admin={admin.email} roles={len(roles_by_name)}")


if __name__ == "__main__":
    asyncio.run(seed())
