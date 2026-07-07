"""Test harness.

Spins up a dedicated ``aegis_test`` database, applies ALL migrations from
scratch via the real ``alembic upgrade head`` (so the db-integrity story is
genuine), seeds it, and yields an httpx ``AsyncClient`` bound to the ASGI app.
Requires a reachable Postgres (the docker-compose one, or a local cluster);
tests that need it skip cleanly when it is absent.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Point the whole process at the test DB BEFORE app modules import settings.
_ADMIN_DB = os.environ.get("TEST_ADMIN_DB", "postgresql://aegis:aegis@localhost:5432/postgres")
_TEST_DB_NAME = "aegis_test"
_TEST_URL = f"postgresql+asyncpg://aegis:aegis@localhost:5432/{_TEST_DB_NAME}"
os.environ["DATABASE_URL"] = _TEST_URL
os.environ.setdefault("ENVIRONMENT", "development")

import asyncpg  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from asgi_lifespan import LifespanManager  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parents[1]


async def _postgres_reachable() -> bool:
    try:
        conn = await asyncpg.connect(_ADMIN_DB)
        await conn.close()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def prepared_db():
    if not await _postgres_reachable():
        pytest.skip("Postgres not reachable — set up docker-compose db to run these tests.")

    # Fresh test database.
    admin = await asyncpg.connect(_ADMIN_DB)
    try:
        await admin.execute(
            f"""
            SELECT pg_terminate_backend(pid) FROM pg_stat_activity
            WHERE datname = '{_TEST_DB_NAME}' AND pid <> pg_backend_pid();
            """
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB_NAME}";')
        await admin.execute(f'CREATE DATABASE "{_TEST_DB_NAME}";')
    finally:
        await admin.close()

    # Apply ALL migrations from scratch — the real pipeline.
    env = {**os.environ, "DATABASE_URL": _TEST_URL}
    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"alembic upgrade failed:\n{result.stderr}"

    # Seed.
    from app.db.seed import seed

    await seed()
    yield

    # Dispose the app engine on the session loop so GC doesn't try to close
    # asyncpg connections on an already-closed loop.
    from app.db.session import engine

    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def client(prepared_db):
    from app.main import app

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
