"""Allow the canonical Life graph to store typed Finance records.

Revision ID: 20260723_0008
Revises: 20260722_0007

Finance data stays in encrypted, Account.id-owned ``life_entities`` rows.  No
bank credentials, executor state, or external banking connection is added by
this migration; it only widens the frozen entity discriminator constraint.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260723_0008"
down_revision: Union[str, Sequence[str], None] = "20260722_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PREVIOUS_LIFE_ENTITY_TYPES = (
    "person", "area", "goal", "project", "milestone", "task", "action",
    "event", "communication_thread", "message", "note", "file",
    "decision", "habit", "metric", "transaction", "health_record",
    "place", "asset", "reminder", "automation", "source",
    "journal_entry", "workspace", "trip", "interaction", "commitment",
    "period_review", "learning_record", "career_item", "home_record",
)
FINANCE_LIFE_ENTITY_TYPE = "finance_record"
CURRENT_LIFE_ENTITY_TYPES = PREVIOUS_LIFE_ENTITY_TYPES + (FINANCE_LIFE_ENTITY_TYPE,)

_SQLITE_NAMING_CONVENTION = {
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _constraint_expression(entity_types: Sequence[str]) -> str:
    allowed = ", ".join(repr(value) for value in entity_types)
    return f"entity_type IN ({allowed})"


def _set_sqlite_foreign_keys(enabled: bool) -> None:
    context = op.get_context()
    with context.autocommit_block():
        op.get_bind().exec_driver_sql(
            "PRAGMA foreign_keys=" + ("ON" if enabled else "OFF")
        )


def _replace_life_entity_type_constraint(entity_types: Sequence[str]) -> None:
    bind = op.get_bind()
    expression = _constraint_expression(entity_types)
    if bind.dialect.name == "sqlite":
        _set_sqlite_foreign_keys(False)
        try:
            with op.batch_alter_table(
                "life_entities",
                recreate="always",
                naming_convention=_SQLITE_NAMING_CONVENTION,
            ) as batch:
                batch.drop_constraint("ck_life_entities_type", type_="check")
                batch.create_check_constraint("ck_life_entities_type", expression)
        finally:
            _set_sqlite_foreign_keys(True)
        return
    op.drop_constraint(
        "ck_life_entities_type", "life_entities", type_="check"
    )
    op.create_check_constraint(
        "ck_life_entities_type", "life_entities", expression
    )


def upgrade() -> None:
    _replace_life_entity_type_constraint(CURRENT_LIFE_ENTITY_TYPES)


def downgrade() -> None:
    bind = op.get_bind()
    finance_count = int(bind.execute(sa.text(
        "SELECT COUNT(*) FROM life_entities WHERE entity_type = :entity_type"
    ), {"entity_type": FINANCE_LIFE_ENTITY_TYPE}).scalar() or 0)
    if finance_count:
        raise RuntimeError(
            "Cannot downgrade finance authority while finance_record rows exist; "
            "export or remove those records explicitly first"
        )
    _replace_life_entity_type_constraint(PREVIOUS_LIFE_ENTITY_TYPES)
