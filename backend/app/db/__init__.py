"""Data-access layer.

The single package that owns the SQLAlchemy engine, sessions, the shared
entities, and ``log_audit()``. No module constructs its own engine, and no
raw SQL lives outside Alembic migrations. The frontend never touches this.
"""

from app.db.base import Base
from app.db.session import get_session, get_sessionmaker, engine

__all__ = ["Base", "get_session", "get_sessionmaker", "engine"]
