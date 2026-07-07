"""GIN full-text-search indexes for GraphRAG hybrid retrieval.

Expression indexes matching the exact to_tsvector() expressions used by
app/db/graphrag.py, so anchor resolution and in-subgraph ranking stay
index-backed as data grows. Named ``*_fts`` — migrations/env.py excludes
that suffix from autogenerate so these are never flagged for drops (they
have no ORM-side declaration).

Revision ID: 0005_fts_indexes
Revises: 0da74f905cfe
Create Date: 2026-07-07
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0005_fts_indexes"
down_revision: Union[str, None] = "0da74f905cfe"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_counterparty_name_fts
          ON counterparty USING gin (to_tsvector('english', name));
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_person_name_email_fts
          ON person USING gin (to_tsvector('english',
              coalesce(name,'') || ' ' || coalesce(email,'')));
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_document_text_fts
          ON document USING gin (to_tsvector('english',
              coalesce(name,'') || ' ' || coalesce(extracted_text,'')));
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_intake_ticket_description_fts
          ON intake_ticket USING gin (to_tsvector('english', description));
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_intake_ticket_description_fts;")
    op.execute("DROP INDEX IF EXISTS ix_document_text_fts;")
    op.execute("DROP INDEX IF EXISTS ix_person_name_email_fts;")
    op.execute("DROP INDEX IF EXISTS ix_counterparty_name_fts;")
