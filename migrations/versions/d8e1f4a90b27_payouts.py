"""Выплаты партнёрам: payout_requests + category у тикетов.

- support_tickets.category (support/payout) — отдельная вкладка «Выплаты»
- payout_requests — заявки на вывод реферальных начислений

Revision ID: d8e1f4a90b27
Revises: b4f2a0c7d901
Create Date: 2026-08-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "d8e1f4a90b27"
down_revision: str | None = "b4f2a0c7d901"
branch_labels: str | None = None
depends_on: str | None = None


def _has_column(conn, table: str, column: str) -> bool:
    return column in {row[1] for row in conn.execute(sa.text(f"PRAGMA table_info({table})"))}


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"), {"n": name}
        ).first()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, "support_tickets", "category"):
        with op.batch_alter_table("support_tickets") as batch:
            batch.add_column(
                sa.Column("category", sa.String(length=16), nullable=False, server_default="support")
            )
        op.create_index("ix_support_tickets_category", "support_tickets", ["category"])

    if not _has_table(conn, "payout_requests"):
        op.create_table(
            "payout_requests",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("amount_kopeks", sa.Integer(), nullable=False),
            sa.Column("contact", sa.String(length=255), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
            sa.Column("admin_comment", sa.Text(), nullable=True),
            sa.Column("ticket_id", sa.Integer(), nullable=True),
            sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["ticket_id"], ["support_tickets.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_payout_requests_user_id", "payout_requests", ["user_id"])
        op.create_index("ix_payout_requests_status", "payout_requests", ["status"])


def downgrade() -> None:
    op.drop_table("payout_requests")
    op.drop_index("ix_support_tickets_category", table_name="support_tickets")
    with op.batch_alter_table("support_tickets") as batch:
        batch.drop_column("category")
