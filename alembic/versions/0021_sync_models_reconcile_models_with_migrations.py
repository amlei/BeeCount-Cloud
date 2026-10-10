"""reconcile models with migrations

Revision ID: 0021_sync_models
Revises: 0020_category_parent_links
Create Date: 2026-10-10 23:11:26.204043
"""

import sqlalchemy as sa
from alembic import op

revision = "0021_sync_models"
down_revision = "0020_category_parent_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Models declare Alembic's naming convention for these indexes; align the
    # names created by 0012.
    op.drop_index("ix_invites_expires_at", table_name="ledger_invites")
    op.drop_index("ix_invites_ledger_id", table_name="ledger_invites")
    op.create_index(
        op.f("ix_ledger_invites_expires_at"),
        "ledger_invites",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ledger_invites_ledger_id"),
        "ledger_invites",
        ["ledger_id"],
        unique=False,
    )

    # models.py intentionally allows every protocol tx_type (32 chars).
    with op.batch_alter_table("read_tx_projection") as batch_op:
        batch_op.alter_column(
            "tx_type",
            existing_type=sa.String(length=16),
            type_=sa.String(length=32),
            existing_nullable=False,
        )

    op.create_index(
        op.f("ix_sync_changes_scope"),
        "sync_changes",
        ["scope"],
        unique=False,
    )

    # Add defaults that models declare but early projection migrations omitted.
    with op.batch_alter_table("read_tx_projection") as batch_op:
        batch_op.alter_column(
            "source_change_id",
            existing_type=sa.BigInteger(),
            server_default="0",
            existing_nullable=False,
        )
    with op.batch_alter_table("read_budget_projection") as batch_op:
        batch_op.alter_column(
            "enabled",
            existing_type=sa.Boolean(),
            server_default=sa.true(),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "source_change_id",
            existing_type=sa.BigInteger(),
            server_default="0",
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("read_budget_projection") as batch_op:
        batch_op.alter_column(
            "enabled",
            existing_type=sa.Boolean(),
            server_default=None,
            existing_nullable=False,
        )
        batch_op.alter_column(
            "source_change_id",
            existing_type=sa.BigInteger(),
            server_default=None,
            existing_nullable=False,
        )
    with op.batch_alter_table("read_tx_projection") as batch_op:
        batch_op.alter_column(
            "source_change_id",
            existing_type=sa.BigInteger(),
            server_default=None,
            existing_nullable=False,
        )
    op.drop_index(op.f("ix_sync_changes_scope"), table_name="sync_changes")
    with op.batch_alter_table("read_tx_projection") as batch_op:
        batch_op.alter_column(
            "tx_type",
            existing_type=sa.String(length=32),
            type_=sa.String(length=16),
            existing_nullable=False,
        )
    op.drop_index(
        op.f("ix_ledger_invites_ledger_id"), table_name="ledger_invites"
    )
    op.drop_index(
        op.f("ix_ledger_invites_expires_at"), table_name="ledger_invites"
    )
    op.create_index(
        "ix_invites_ledger_id", "ledger_invites", ["ledger_id"], unique=False
    )
    op.create_index(
        "ix_invites_expires_at", "ledger_invites", ["expires_at"], unique=False
    )
