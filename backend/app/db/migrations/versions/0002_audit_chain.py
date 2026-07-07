"""AuditLog cryptographic chain (D11) — append-only + hash-linked.

Ported from the frontend's Prisma migration ``20260501120100_step4a_audit_chain``,
adjusted to this backend's snake_case identifiers. Once this lands, no path
through SQLAlchemy or raw SQL can mutate or remove an ``audit_log`` row, and
any post-hoc tampering breaks the SHA-256 chain so ``verify_audit_chain`` can
localise the break.

Order of operations:
  1. pgcrypto (digest()).
  2. Canonical-content + hash helper functions (IMMUTABLE).
  3. Backfill chain_position / prev_hash / content_hash for any pre-existing
     rows (none on a fresh DB, kept for re-runnability against seeded data).
  4. Per-org unique index on chain_position.
  5. BEFORE INSERT trigger — fills prev_hash/chain_position/content_hash under
     a per-org advisory lock so concurrent inserts cannot collide.
  6. BEFORE UPDATE / DELETE triggers — raise unconditionally.

Revision ID: 0002_audit_chain
Revises: fcdad2d98ec6
Create Date: 2026-07-07
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0002_audit_chain"
down_revision: Union[str, None] = "fcdad2d98ec6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")

    # ── 2. Canonical-content + hash functions ───────────────────────
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION audit_log_canonical_content(
          p_schema_version int,
          p_org_id text,
          p_actor_id text,
          p_actor_type text,
          p_action text,
          p_resource_type text,
          p_resource_id text,
          p_before_json jsonb,
          p_after_json jsonb,
          p_metadata jsonb,
          p_timestamp timestamp,
          p_prev_hash text,
          p_chain_position bigint
        ) RETURNS text
        LANGUAGE sql IMMUTABLE AS $$
          SELECT
            'v=' || p_schema_version::text || E'\n' ||
            'org=' || p_org_id || E'\n' ||
            'actor=' || COALESCE(p_actor_id, '') || E'\n' ||
            'actor_type=' || p_actor_type || E'\n' ||
            'action=' || p_action || E'\n' ||
            'rtype=' || p_resource_type || E'\n' ||
            'rid=' || p_resource_id || E'\n' ||
            'before=' || COALESCE(p_before_json::text, 'null') || E'\n' ||
            'after=' || COALESCE(p_after_json::text, 'null') || E'\n' ||
            'meta=' || COALESCE(p_metadata::text, 'null') || E'\n' ||
            'ts=' || to_char(p_timestamp, 'YYYY-MM-DD"T"HH24:MI:SS.US') || E'\n' ||
            'prev=' || p_prev_hash || E'\n' ||
            'pos=' || p_chain_position::text
        $$;
        """
    )
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION audit_log_compute_hash(
          p_schema_version int,
          p_org_id text,
          p_actor_id text,
          p_actor_type text,
          p_action text,
          p_resource_type text,
          p_resource_id text,
          p_before_json jsonb,
          p_after_json jsonb,
          p_metadata jsonb,
          p_timestamp timestamp,
          p_prev_hash text,
          p_chain_position bigint
        ) RETURNS text
        LANGUAGE sql IMMUTABLE AS $$
          SELECT encode(
            digest(
              audit_log_canonical_content(
                p_schema_version, p_org_id, p_actor_id, p_actor_type, p_action,
                p_resource_type, p_resource_id, p_before_json, p_after_json,
                p_metadata, p_timestamp, p_prev_hash, p_chain_position
              ),
              'sha256'
            ),
            'hex'
          )
        $$;
        """
    )

    # ── 3. Backfill any pre-existing rows (recursive per-org walk) ───
    op.execute(
        r"""
        WITH RECURSIVE ordered AS (
          SELECT
            al.id, al.organization_id, al.schema_version, al.actor_id,
            al.actor_type, al.action, al.resource_type, al.resource_id,
            al.before_json, al.after_json, al.metadata, al.timestamp,
            ROW_NUMBER() OVER (
              PARTITION BY al.organization_id
              ORDER BY al.timestamp ASC, al.id ASC
            ) AS pos
          FROM audit_log al
        ),
        chain AS (
          SELECT
            o.*,
            repeat('0', 64) AS prev_hash,
            audit_log_compute_hash(
              o.schema_version, o.organization_id, o.actor_id, o.actor_type,
              o.action, o.resource_type, o.resource_id,
              o.before_json, o.after_json, o.metadata,
              o.timestamp, repeat('0', 64), o.pos
            ) AS content_hash
          FROM ordered o
          WHERE o.pos = 1
          UNION ALL
          SELECT
            o.*,
            c.content_hash AS prev_hash,
            audit_log_compute_hash(
              o.schema_version, o.organization_id, o.actor_id, o.actor_type,
              o.action, o.resource_type, o.resource_id,
              o.before_json, o.after_json, o.metadata,
              o.timestamp, c.content_hash, o.pos
            ) AS content_hash
          FROM ordered o
          JOIN chain c
            ON c.organization_id = o.organization_id
           AND o.pos = c.pos + 1
        )
        UPDATE audit_log al
        SET prev_hash = chain.prev_hash,
            content_hash = chain.content_hash,
            chain_position = chain.pos
        FROM chain
        WHERE al.id = chain.id;
        """
    )

    # ── 4. Per-org unique index on chain_position ────────────────────
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_log_org_chain_position
          ON audit_log (organization_id, chain_position);
        """
    )

    # ── 5. BEFORE INSERT trigger — chain the new row ─────────────────
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION audit_log_before_insert()
        RETURNS TRIGGER
        LANGUAGE plpgsql AS $$
        DECLARE
          v_lock_key bigint;
          v_prev_pos bigint;
          v_prev_hash text;
        BEGIN
          v_lock_key := hashtextextended(NEW.organization_id, 0);
          PERFORM pg_advisory_xact_lock(v_lock_key);

          SELECT chain_position, content_hash
            INTO v_prev_pos, v_prev_hash
          FROM audit_log
          WHERE organization_id = NEW.organization_id
          ORDER BY chain_position DESC
          LIMIT 1;

          IF NOT FOUND THEN
            v_prev_pos := 0;
            v_prev_hash := repeat('0', 64);
          END IF;

          -- Apps cannot influence these — even if a default is sent, we
          -- overwrite. This makes tampering-on-insert impossible.
          NEW.chain_position := v_prev_pos + 1;
          NEW.prev_hash := v_prev_hash;
          NEW.content_hash := audit_log_compute_hash(
            NEW.schema_version, NEW.organization_id, NEW.actor_id,
            NEW.actor_type, NEW.action, NEW.resource_type, NEW.resource_id,
            NEW.before_json, NEW.after_json, NEW.metadata,
            NEW.timestamp, NEW.prev_hash, NEW.chain_position
          );
          RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_chain_insert
          BEFORE INSERT ON audit_log
          FOR EACH ROW
          EXECUTE FUNCTION audit_log_before_insert();
        """
    )

    # ── 6. Immutability — block UPDATE and DELETE ────────────────────
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION audit_log_immutable()
        RETURNS TRIGGER
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION
            'audit_log is append-only and cryptographically chained. % is forbidden.',
            TG_OP
            USING
              ERRCODE = 'check_violation',
              HINT = 'Audit rows record what happened. Add a corrective row instead.';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_no_update
          BEFORE UPDATE ON audit_log
          FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_no_delete
          BEFORE DELETE ON audit_log
          FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_delete ON audit_log;")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log;")
    op.execute("DROP TRIGGER IF EXISTS audit_log_chain_insert ON audit_log;")
    op.execute("DROP FUNCTION IF EXISTS audit_log_immutable();")
    op.execute("DROP FUNCTION IF EXISTS audit_log_before_insert();")
    op.execute("DROP INDEX IF EXISTS uq_audit_log_org_chain_position;")
    op.execute(
        "DROP FUNCTION IF EXISTS audit_log_compute_hash("
        "int,text,text,text,text,text,text,jsonb,jsonb,jsonb,timestamp,text,bigint);"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS audit_log_canonical_content("
        "int,text,text,text,text,text,text,jsonb,jsonb,jsonb,timestamp,text,bigint);"
    )
