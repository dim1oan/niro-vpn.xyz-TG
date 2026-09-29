"""Верификация email: users.email_verified + таблица email_tokens.

Существующие аккаунты с паролем считаем доверенными (созданы до ввода
верификации либо активированы кодом из бота) — бэкфилл email_verified=1.

Revision ID: f3a7c2d84e19
Revises: d8e1f4a90b27
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "f3a7c2d84e19"
down_revision: str | None = "d8e1f4a90b27"
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
    if not _has_column(conn, "users", "email_verified"):
        with op.batch_alter_table("users") as batch:
            batch.add_column(
                sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.false())
            )
        # доверяем уже существующим аккаунтам с паролем (активация кодом / старые регистрации)
        conn.exec_driver_sql("UPDATE users SET email_verified = 1 WHERE password_hash IS NOT NULL")

    if not _has_table(conn, "email_tokens"):
        op.create_table(
            "email_tokens",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("token", sa.String(length=64), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_email_tokens_token", "email_tokens", ["token"], unique=True)
        op.create_index("ix_email_tokens_user_id", "email_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_table("email_tokens")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("email_verified")
