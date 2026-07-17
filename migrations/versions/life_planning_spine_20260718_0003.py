"""Add Restia's principal-scoped Life OS planning spine.

Revision ID: 20260718_0003
Revises: 20260717_0002
"""

from __future__ import annotations

import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260718_0003"
down_revision: Union[str, Sequence[str], None] = "20260717_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


LIFE_ENTITY_TYPES = (
    "person", "area", "goal", "project", "milestone", "task", "action",
    "event", "communication_thread", "message", "note", "file",
    "decision", "habit", "metric", "transaction", "health_record",
    "place", "asset", "reminder", "automation", "source",
    "journal_entry", "workspace", "trip", "interaction", "commitment",
    "period_review", "learning_record", "career_item", "home_record",
)

V3_REQUIRED_TABLES = frozenset({
    "action_policies", "action_proposals", "focus_sessions", "life_entities",
    "life_entity_versions", "life_sources",
})

V3_REQUIRED_COLUMNS = {
    "life_sources": frozenset({
        "id", "owner_id", "source_type", "title", "source_ref",
        "safe_excerpt", "content_sha256", "observed_at", "captured_at",
        "sensitivity", "metadata", "idempotency_key", "version",
        "created_at", "updated_at",
    }),
    "life_entities": frozenset({
        "id", "owner_id", "entity_type", "title", "summary", "status",
        "properties", "provenance", "confidence", "sensitivity",
        "domain_ref_type", "domain_ref_id", "occurred_at", "due_at",
        "review_at", "idempotency_key", "version", "deleted_at",
        "created_at", "updated_at",
    }),
    "life_entity_versions": frozenset({
        "id", "owner_id", "entity_id", "version", "snapshot", "reason",
        "created_at",
    }),
    "entity_links": frozenset({
        "id", "owner_id", "source_type", "source_id", "relation",
        "target_type", "target_id", "metadata", "provenance", "confidence",
        "sensitivity", "version", "deleted_at", "created_at", "updated_at",
    }),
    "action_policies": frozenset({
        "id", "owner_id", "domain", "max_autonomy",
        "external_requires_confirmation", "enabled", "rules", "version",
        "created_at", "updated_at",
    }),
    "action_proposals": frozenset({
        "id", "owner_id", "domain", "action", "autonomy_level", "state",
        "target_type", "target_id", "payload", "reason", "sources",
        "external", "requires_confirmation", "confirmation_digest", "expires_at",
        "approved_at", "approved_by_account_id", "executed_at", "result",
        "undo_ref", "idempotency_key", "version", "created_at", "updated_at",
    }),
    "focus_sessions": frozenset({
        "id", "owner_id", "entity_id", "state", "definition_of_done",
        "started_at", "active_since", "paused_at", "completed_at", "elapsed_seconds",
        "interruptions", "progress", "evidence", "follow_up_entity_ids",
        "version", "created_at", "updated_at",
    }),
}


def _require_online_execution() -> None:
    """Refuse SQL-only generation for this key-dependent revision.

    Emitting comments while allowing Alembic to stamp the revision would leave
    legacy planning text and EntityLink private JSON in plaintext. Revision 0003
    therefore has an explicit online-only contract: the deployment key must be
    present and the rewrite must execute before the head can be recorded.
    """
    if bool(getattr(op.get_context(), "as_sql", False)):
        raise RuntimeError(
            "Revision 20260718_0003 requires an online Alembic migration "
            "with the Restia deployment encryption key; offline SQL is unsafe"
        )


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )


def _install_version_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("""
            CREATE TRIGGER IF NOT EXISTS life_entity_versions_no_update
            BEFORE UPDATE ON life_entity_versions
            BEGIN
                SELECT RAISE(ABORT, 'LifeEntityVersion rows are append-only');
            END
        """)
        op.execute("""
            CREATE TRIGGER IF NOT EXISTS life_entity_versions_no_delete
            BEFORE DELETE ON life_entity_versions
            BEGIN
                SELECT RAISE(ABORT, 'LifeEntityVersion rows are append-only');
            END
        """)
    elif dialect == "postgresql":
        op.execute("""
            CREATE OR REPLACE FUNCTION restia_reject_life_entity_version_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'LifeEntityVersion rows are append-only';
            END;
            $$ LANGUAGE plpgsql
        """)
        op.execute("""
            CREATE TRIGGER life_entity_versions_no_update_or_delete
            BEFORE UPDATE OR DELETE ON life_entity_versions
            FOR EACH ROW EXECUTE FUNCTION restia_reject_life_entity_version_mutation()
        """)


def _install_entity_link_guards() -> None:
    """Match the model's invariants on the pre-existing edge table.

    SQLite cannot add named CHECK constraints without rebuilding the whole
    table.  Equivalent INSERT/UPDATE triggers keep the additive migration
    safe.  PostgreSQL can install the actual named constraints.  SQLite also
    needs a trigger to replace the constant default required while adding a
    non-null column to a populated table.
    """

    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("""
            CREATE TRIGGER IF NOT EXISTS entity_links_validate_insert
            BEFORE INSERT ON entity_links
            WHEN NEW.confidence < 0 OR NEW.confidence > 100 OR NEW.version < 1
            BEGIN
                SELECT RAISE(ABORT, 'EntityLink confidence/version is invalid');
            END
        """)
        op.execute("""
            CREATE TRIGGER IF NOT EXISTS entity_links_validate_update
            BEFORE UPDATE ON entity_links
            WHEN NEW.confidence < 0 OR NEW.confidence > 100 OR NEW.version < 1
            BEGIN
                SELECT RAISE(ABORT, 'EntityLink confidence/version is invalid');
            END
        """)
        op.execute("""
            CREATE TRIGGER IF NOT EXISTS entity_links_fill_updated_at
            AFTER INSERT ON entity_links
            WHEN NEW.updated_at = '1970-01-01 00:00:00'
            BEGIN
                UPDATE entity_links
                SET updated_at = CURRENT_TIMESTAMP
                WHERE id = NEW.id;
            END
        """)
    elif dialect == "postgresql":
        op.create_check_constraint(
            "ck_entity_links_confidence",
            "entity_links",
            "confidence >= 0 AND confidence <= 100",
        )
        op.create_check_constraint(
            "ck_entity_links_version",
            "entity_links",
            "version >= 1",
        )
        op.alter_column(
            "entity_links",
            "updated_at",
            existing_type=sa.DateTime(),
            existing_nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        )


def _remove_entity_link_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS entity_links_fill_updated_at")
        op.execute("DROP TRIGGER IF EXISTS entity_links_validate_update")
        op.execute("DROP TRIGGER IF EXISTS entity_links_validate_insert")
    elif dialect == "postgresql":
        op.drop_constraint(
            "ck_entity_links_version", "entity_links", type_="check"
        )
        op.drop_constraint(
            "ck_entity_links_confidence", "entity_links", type_="check"
        )


def _rewrite_entity_link_encrypted_json(
    *, encrypt_at_rest: bool, retained_only: bool = False
) -> None:
    """Rewrite retained private edge JSON across the EncryptedJSON boundary.

    Fernet requires the deployment key and therefore runs only during an
    online migration. Revision-level guards reject offline SQL generation so a
    plaintext database can never be stamped as migrated.
    """

    from src.secret_storage import (
        decrypt,
        encrypt_plaintext,
        is_decryptable,
        is_encrypted,
    )

    bind = op.get_bind()
    links = sa.table(
        "entity_links",
        sa.column("id", sa.String(length=36)),
        sa.column("source_type", sa.String(length=48)),
        sa.column("target_type", sa.String(length=48)),
        sa.column("metadata", sa.JSON()),
        sa.column("provenance", sa.JSON()),
    )
    rows = bind.execute(sa.select(
        links.c.id,
        links.c.source_type,
        links.c.target_type,
        links.c.metadata,
        links.c.provenance,
    )).all()
    for row_id, source_type, target_type, stored_metadata, stored_provenance in rows:
        if (
            retained_only
            and (source_type == "life_entity" or target_type == "life_entity")
        ):
            continue
        updates: dict[str, object] = {}
        for column_name, stored in (
            ("metadata", stored_metadata),
            ("provenance", stored_provenance),
        ):
            if encrypt_at_rest:
                if isinstance(stored, str) and is_encrypted(stored):
                    if not is_decryptable(stored):
                        raise RuntimeError(
                            f"EntityLink {column_name} could not be decrypted with the active key"
                        )
                    continue
                if isinstance(stored, str) and stored.startswith("enc:"):
                    raise RuntimeError(
                        f"EntityLink {column_name} has an invalid encrypted envelope"
                    )
                if not isinstance(stored, dict):
                    raise RuntimeError(
                        f"EntityLink {column_name} must be a JSON object before encryption"
                    )
                serialized = json.dumps(
                    stored, ensure_ascii=False, separators=(",", ":")
                )
                updates[column_name] = encrypt_plaintext(serialized)
                continue

            if isinstance(stored, dict):
                continue
            if not isinstance(stored, str) or not stored.startswith("enc:"):
                raise RuntimeError(
                    f"EntityLink {column_name} must be an encrypted JSON object before downgrade"
                )
            if not is_decryptable(stored):
                raise RuntimeError(
                    f"EntityLink {column_name} could not be decrypted with the active key"
                )
            try:
                rewritten = json.loads(decrypt(stored))
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"EntityLink {column_name} could not be decrypted for downgrade"
                ) from exc
            if not isinstance(rewritten, dict):
                raise RuntimeError(
                    f"EntityLink {column_name} must decrypt to a JSON object"
                )
            updates[column_name] = rewritten
        if updates:
            bind.execute(
                links.update().where(links.c.id == row_id).values(**updates)
            )


def _rewrite_planning_item_text(*, encrypt_at_rest: bool) -> None:
    """Rewrite V2 planning text across the V3 EncryptedText boundary.

    The key-dependent rewrite must run online.  Downgrade validates every row
    before issuing any UPDATE so a missing/rotated key or a title that no
    longer fits V2's 240-character contract leaves the database untouched.
    """

    from src.secret_storage import (
        decrypt,
        encrypt_plaintext,
        is_content_encrypted,
        is_decryptable,
        is_encrypted,
    )

    bind = op.get_bind()
    planning_items = sa.table(
        "planning_items",
        sa.column("id", sa.String(length=36)),
        sa.column("title", sa.Text()),
        sa.column("details", sa.Text()),
    )
    rows = bind.execute(sa.select(
        planning_items.c.id,
        planning_items.c.title,
        planning_items.c.details,
    )).all()
    rewrites: list[tuple[str, str, str]] = []
    for row_id, stored_title, stored_details in rows:
        title = "" if stored_title is None else str(stored_title)
        details = "" if stored_details is None else str(stored_details)
        if encrypt_at_rest:
            # Revision 0002 defines both columns as plaintext content. Old
            # credential-style `enc:<fernet>` strings are therefore literal
            # user data and get wrapped. Only V3's distinct `enc:c1:` content
            # envelope is idempotent, which also makes a partially completed
            # online rewrite safe to retry.
            for field, stored in (("title", title), ("details", details)):
                if is_content_encrypted(stored) and not is_decryptable(stored):
                    raise RuntimeError(
                        f"PlanningItem {field} could not be decrypted with the active key"
                    )
            rewritten_title = (
                title if is_content_encrypted(title) else encrypt_plaintext(title)
            )
            rewritten_details = (
                details if is_content_encrypted(details) else encrypt_plaintext(details)
            )
        else:
            for field, stored in (("title", title), ("details", details)):
                if is_encrypted(stored) and not is_decryptable(stored):
                    raise RuntimeError(
                        f"PlanningItem {field} could not be decrypted with the active key"
                    )
            rewritten_title = decrypt(title)
            rewritten_details = decrypt(details)
            if title.startswith("enc:") and not rewritten_title:
                raise RuntimeError(
                    "PlanningItem title could not be decrypted for downgrade"
                )
            if details.startswith("enc:") and not rewritten_details:
                raise RuntimeError(
                    "PlanningItem details could not be decrypted for downgrade"
                )
            if len(rewritten_title) > 240:
                raise RuntimeError(
                    "PlanningItem title exceeds the V2 240-character rollback limit"
                )
        if (rewritten_title, rewritten_details) != (title, details):
            rewrites.append((str(row_id), rewritten_title, rewritten_details))

    for row_id, rewritten_title, rewritten_details in rewrites:
        bind.execute(
            planning_items.update()
            .where(planning_items.c.id == row_id)
            .values(title=rewritten_title, details=rewritten_details)
        )


def upgrade() -> None:
    _require_online_execution()
    # V2 declared titles as VARCHAR(240). Fernet envelopes can exceed that
    # even when the plaintext obeys the legacy limit, so widen PostgreSQL
    # before rewriting. SQLite does not enforce the declared VARCHAR width.
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column(
            "planning_items",
            "title",
            existing_type=sa.String(length=240),
            type_=sa.Text(),
            existing_nullable=False,
        )
    _rewrite_planning_item_text(encrypt_at_rest=True)

    op.create_table(
        "life_sources",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("source_type", sa.String(length=48), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("safe_excerpt", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("observed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "captured_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("sensitivity", sa.String(length=24), nullable=False, server_default="private"),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint("version >= 1", name="ck_life_sources_version"),
        sa.ForeignKeyConstraint(["owner_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "idempotency_key", name="uq_life_sources_owner_idempotency"),
    )
    op.create_index("ix_life_sources_owner_id", "life_sources", ["owner_id"])
    op.create_index("ix_life_sources_source_type", "life_sources", ["source_type"])
    op.create_index("ix_life_sources_content_sha256", "life_sources", ["content_sha256"])
    op.create_index("ix_life_sources_observed_at", "life_sources", ["observed_at"])
    op.create_index("ix_life_sources_captured_at", "life_sources", ["captured_at"])
    op.create_index(
        "ix_life_sources_owner_type_captured", "life_sources",
        ["owner_id", "source_type", "captured_at"],
    )

    allowed_types = ", ".join(repr(value) for value in LIFE_ENTITY_TYPES)
    op.create_table(
        "life_entities",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("entity_type", sa.String(length=48), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("properties", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("provenance", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("confidence", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("sensitivity", sa.String(length=24), nullable=False, server_default="private"),
        sa.Column("domain_ref_type", sa.String(length=48), nullable=True),
        sa.Column("domain_ref_id", sa.String(length=255), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=True),
        sa.Column("due_at", sa.DateTime(), nullable=True),
        sa.Column("review_at", sa.DateTime(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            f"entity_type IN ({allowed_types})", name="ck_life_entities_type",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 100",
            name="ck_life_entities_confidence",
        ),
        sa.CheckConstraint("version >= 1", name="ck_life_entities_version"),
        sa.ForeignKeyConstraint(["owner_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "owner_id", name="uq_life_entities_id_owner"),
        sa.UniqueConstraint("owner_id", "idempotency_key", name="uq_life_entities_owner_idempotency"),
        sa.UniqueConstraint(
            "owner_id", "entity_type", "domain_ref_type", "domain_ref_id",
            name="uq_life_entities_domain_ref",
        ),
    )
    op.create_index("ix_life_entities_owner_id", "life_entities", ["owner_id"])
    op.create_index("ix_life_entities_entity_type", "life_entities", ["entity_type"])
    op.create_index("ix_life_entities_status", "life_entities", ["status"])
    op.create_index("ix_life_entities_occurred_at", "life_entities", ["occurred_at"])
    op.create_index("ix_life_entities_due_at", "life_entities", ["due_at"])
    op.create_index("ix_life_entities_review_at", "life_entities", ["review_at"])
    op.create_index("ix_life_entities_deleted_at", "life_entities", ["deleted_at"])
    op.create_index(
        "ix_life_entities_owner_type_status", "life_entities",
        ["owner_id", "entity_type", "status"],
    )
    op.create_index(
        "ix_life_entities_owner_updated", "life_entities", ["owner_id", "updated_at"],
    )

    op.create_table(
        "life_entity_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("version >= 1", name="ck_life_entity_versions_version"),
        sa.ForeignKeyConstraint(
            ["entity_id", "owner_id"],
            ["life_entities.id", "life_entities.owner_id"],
            ondelete="CASCADE",
            name="fk_life_entity_versions_entity_owner",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entity_id", "version", name="uq_life_entity_versions_entity_version"),
    )
    op.create_index("ix_life_entity_versions_owner_id", "life_entity_versions", ["owner_id"])
    op.create_index("ix_life_entity_versions_entity_id", "life_entity_versions", ["entity_id"])
    op.create_index("ix_life_entity_versions_created_at", "life_entity_versions", ["created_at"])
    op.create_index(
        "ix_life_entity_versions_owner_created", "life_entity_versions",
        ["owner_id", "created_at"],
    )
    _install_version_guards()

    op.add_column(
        "entity_links",
        sa.Column("provenance", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column(
        "entity_links",
        sa.Column("confidence", sa.Integer(), nullable=False, server_default="100"),
    )
    op.add_column(
        "entity_links",
        sa.Column("sensitivity", sa.String(length=24), nullable=False, server_default="private"),
    )
    op.add_column(
        "entity_links",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("entity_links", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    op.add_column(
        "entity_links",
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False,
            # SQLite rejects CURRENT_TIMESTAMP while adding a column to a
            # populated table. Use a constant migration default, then backfill
            # every existing row to the actual upgrade time below.
            server_default=sa.text("'1970-01-01 00:00:00'"),
        ),
    )
    op.execute(
        "UPDATE entity_links SET updated_at = CURRENT_TIMESTAMP "
        "WHERE updated_at = '1970-01-01 00:00:00'"
    )
    op.create_index("ix_entity_links_deleted_at", "entity_links", ["deleted_at"])
    _install_entity_link_guards()
    _rewrite_entity_link_encrypted_json(encrypt_at_rest=True)

    op.create_table(
        "action_policies",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("domain", sa.String(length=48), nullable=False),
        sa.Column("max_autonomy", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("external_requires_confirmation", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("rules", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "max_autonomy >= 1 AND max_autonomy <= 6",
            name="ck_action_policies_autonomy",
        ),
        sa.CheckConstraint("version >= 1", name="ck_action_policies_version"),
        sa.ForeignKeyConstraint(["owner_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "domain", name="uq_action_policies_owner_domain"),
    )
    op.create_index("ix_action_policies_owner_id", "action_policies", ["owner_id"])

    op.create_table(
        "action_proposals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("domain", sa.String(length=48), nullable=False),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("autonomy_level", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False, server_default="prepared"),
        sa.Column("target_type", sa.String(length=48), nullable=False),
        sa.Column("target_id", sa.String(length=255), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("external", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("requires_confirmation", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("confirmation_digest", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("approved_by_account_id", sa.String(length=36), nullable=True),
        sa.Column("executed_at", sa.DateTime(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("undo_ref", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "autonomy_level >= 1 AND autonomy_level <= 6",
            name="ck_action_proposals_autonomy",
        ),
        sa.CheckConstraint(
            "state IN ('prepared', 'approved', 'executing', 'completed', "
            "'rejected', 'failed', 'reversed', 'expired')",
            name="ck_action_proposals_state",
        ),
        sa.CheckConstraint(
            "approved_by_account_id IS NULL OR approved_by_account_id = owner_id",
            name="ck_action_proposals_approver_owner",
        ),
        sa.CheckConstraint("version >= 1", name="ck_action_proposals_version"),
        sa.ForeignKeyConstraint(["approved_by_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["owner_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("confirmation_digest"),
        sa.UniqueConstraint("owner_id", "idempotency_key", name="uq_action_proposals_owner_idempotency"),
    )
    op.create_index("ix_action_proposals_owner_id", "action_proposals", ["owner_id"])
    op.create_index("ix_action_proposals_domain", "action_proposals", ["domain"])
    op.create_index("ix_action_proposals_action", "action_proposals", ["action"])
    op.create_index("ix_action_proposals_state", "action_proposals", ["state"])
    op.create_index("ix_action_proposals_expires_at", "action_proposals", ["expires_at"])
    op.create_index(
        "ix_action_proposals_owner_state_created", "action_proposals",
        ["owner_id", "state", "created_at"],
    )

    op.create_table(
        "focus_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False, server_default="active"),
        sa.Column("definition_of_done", sa.Text(), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("active_since", sa.DateTime(), nullable=True),
        sa.Column("paused_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("elapsed_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("interruptions", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("progress", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("follow_up_entity_ids", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "state IN ('active', 'paused', 'completed', 'abandoned')",
            name="ck_focus_sessions_state",
        ),
        sa.CheckConstraint("elapsed_seconds >= 0", name="ck_focus_sessions_elapsed"),
        sa.CheckConstraint("version >= 1", name="ck_focus_sessions_version"),
        sa.ForeignKeyConstraint(
            ["entity_id", "owner_id"],
            ["life_entities.id", "life_entities.owner_id"],
            ondelete="CASCADE",
            name="fk_focus_sessions_entity_owner",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_focus_sessions_owner_id", "focus_sessions", ["owner_id"])
    op.create_index("ix_focus_sessions_entity_id", "focus_sessions", ["entity_id"])
    op.create_index("ix_focus_sessions_state", "focus_sessions", ["state"])
    op.create_index("ix_focus_sessions_owner_state", "focus_sessions", ["owner_id", "state"])
    op.create_index(
        "uq_focus_sessions_owner_live",
        "focus_sessions",
        ["owner_id"],
        unique=True,
        sqlite_where=sa.text("state IN ('active', 'paused')"),
        postgresql_where=sa.text("state IN ('active', 'paused')"),
    )


def downgrade() -> None:
    _require_online_execution()
    # Preflight and perform the only key-dependent rewrite before destructive
    # DDL. A wrong/missing key now aborts while every V3 table and edge is still
    # present. Plain JSON remains readable by the V3 EncryptedJSON adapter if a
    # later unrelated downgrade statement fails on SQLite.
    _rewrite_entity_link_encrypted_json(
        encrypt_at_rest=False, retained_only=True
    )
    _rewrite_planning_item_text(encrypt_at_rest=False)

    op.drop_index("uq_focus_sessions_owner_live", table_name="focus_sessions")
    op.drop_index("ix_focus_sessions_owner_state", table_name="focus_sessions")
    op.drop_index("ix_focus_sessions_state", table_name="focus_sessions")
    op.drop_index("ix_focus_sessions_entity_id", table_name="focus_sessions")
    op.drop_index("ix_focus_sessions_owner_id", table_name="focus_sessions")
    op.drop_table("focus_sessions")

    op.drop_index("ix_action_proposals_owner_state_created", table_name="action_proposals")
    op.drop_index("ix_action_proposals_expires_at", table_name="action_proposals")
    op.drop_index("ix_action_proposals_state", table_name="action_proposals")
    op.drop_index("ix_action_proposals_action", table_name="action_proposals")
    op.drop_index("ix_action_proposals_domain", table_name="action_proposals")
    op.drop_index("ix_action_proposals_owner_id", table_name="action_proposals")
    op.drop_table("action_proposals")
    op.drop_index("ix_action_policies_owner_id", table_name="action_policies")
    op.drop_table("action_policies")

    # V2.1 has no LifeEntity authority. Remove those graph-only edges before
    # dropping the nodes, then restore retained legacy edge metadata to plain
    # JSON so the 0002 model can still read it.
    op.execute(
        "DELETE FROM entity_links "
        "WHERE source_type = 'life_entity' OR target_type = 'life_entity'"
    )
    _remove_entity_link_guards()
    op.drop_index("ix_entity_links_deleted_at", table_name="entity_links")
    for column in (
        "updated_at", "deleted_at", "version", "sensitivity", "confidence", "provenance",
    ):
        op.drop_column("entity_links", column)

    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS life_entity_versions_no_update")
        op.execute("DROP TRIGGER IF EXISTS life_entity_versions_no_delete")
    elif dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS life_entity_versions_no_update_or_delete ON life_entity_versions")
        op.execute("DROP FUNCTION IF EXISTS restia_reject_life_entity_version_mutation()")
    op.drop_index("ix_life_entity_versions_owner_created", table_name="life_entity_versions")
    op.drop_index("ix_life_entity_versions_created_at", table_name="life_entity_versions")
    op.drop_index("ix_life_entity_versions_entity_id", table_name="life_entity_versions")
    op.drop_index("ix_life_entity_versions_owner_id", table_name="life_entity_versions")
    op.drop_table("life_entity_versions")

    op.drop_index("ix_life_entities_owner_updated", table_name="life_entities")
    op.drop_index("ix_life_entities_owner_type_status", table_name="life_entities")
    op.drop_index("ix_life_entities_deleted_at", table_name="life_entities")
    op.drop_index("ix_life_entities_review_at", table_name="life_entities")
    op.drop_index("ix_life_entities_due_at", table_name="life_entities")
    op.drop_index("ix_life_entities_occurred_at", table_name="life_entities")
    op.drop_index("ix_life_entities_status", table_name="life_entities")
    op.drop_index("ix_life_entities_entity_type", table_name="life_entities")
    op.drop_index("ix_life_entities_owner_id", table_name="life_entities")
    op.drop_table("life_entities")

    op.drop_index("ix_life_sources_owner_type_captured", table_name="life_sources")
    op.drop_index("ix_life_sources_captured_at", table_name="life_sources")
    op.drop_index("ix_life_sources_observed_at", table_name="life_sources")
    op.drop_index("ix_life_sources_content_sha256", table_name="life_sources")
    op.drop_index("ix_life_sources_source_type", table_name="life_sources")
    op.drop_index("ix_life_sources_owner_id", table_name="life_sources")
    op.drop_table("life_sources")

    if op.get_bind().dialect.name == "postgresql":
        op.alter_column(
            "planning_items",
            "title",
            existing_type=sa.Text(),
            type_=sa.String(length=240),
            existing_nullable=False,
        )
