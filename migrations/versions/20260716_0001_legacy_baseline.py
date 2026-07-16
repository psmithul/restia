"""Stamp-only baseline for the V3 migration foundation.

Revision ID: 20260716_0001
Revises: None
"""

from typing import NoReturn


revision = "20260716_0001"
down_revision = None
branch_labels = ("legacy_sqlite_baseline",)
depends_on = None


def _stamp_only() -> NoReturn:
    raise RuntimeError(
        "The 20260716_0001 legacy baseline is stamp-only. It cannot create or "
        "downgrade a database. Run Restia's explicit local bootstrap, verify "
        "the schema, then use `scripts/odysseus-db stamp-legacy`."
    )


def upgrade() -> None:
    _stamp_only()


def downgrade() -> None:
    _stamp_only()
