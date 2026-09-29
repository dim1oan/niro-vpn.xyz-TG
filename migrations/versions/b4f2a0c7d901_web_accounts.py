"""web accounts: users без Telegram (email/пароль), activation_codes, support_tickets.

Разовая миграция под веб-кабинет (сайт работает с той же БД, своей alembic не имеет).
- users.telegram_id → nullable (NULL = чисто веб-аккаунт)
- users.email / password_hash / is_site_admin
- таблицы activation_codes и support_tickets

Revision ID: b4f2a0c7d901
Revises: a7b3994c1f13
Create Date: 2026-08-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "b4f2a0c7d901"
down_revision: str | None = "a7b3994c1f13"
branch_labels: str | None = None
depends_on: str | None = None


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"), {"n": name}
        ).first()
    )


def _has_column(conn, table: str, column: str) -> bool:
    return column in {row[1] for row in conn.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    conn = op.get_bind()
    with op.batch_alter_table("users") as batch:
        batch.alter_column("telegram_id", existing_type=sa.BigInteger(), nullable=True)
        if not _has_column(conn, "users", "email"):
            batch.add_column(sa.Column("email", sa.String(length=255), nullable=True))
        if not _has_column(conn, "users", "password_hash"):
            batch.add_column(sa.Column("password_hash", sa.String(length=255), nullable=True))
        if not _has_column(conn, "users", "is_site_admin"):
            batch.add_column(
                sa.Column(
                    "is_site_admin", sa.Boolean(), nullable=False, server_default=sa.false()
                )
            )
    conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users (email)"
    )

    if not _has_table(conn, "activation_codes"):
        op.create_table(
            "activation_codes",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("code", sa.String(length=32), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("code"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_activation_codes_code", "activation_codes", ["code"], unique=False)
        op.create_index("ix_activation_codes_user_id", "activation_codes", ["user_id"], unique=False)

    if not _has_table(conn, "support_tickets"):
        op.create_table(
            "support_tickets",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("subject", sa.String(length=255), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
            sa.Column("admin_reply", sa.Text(), nullable=True),
            sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_support_tickets_user_id", "support_tickets", ["user_id"], unique=False)
        op.create_index("ix_support_tickets_status", "support_tickets", ["status"], unique=False)


def downgrade() -> None:
    op.drop_table("support_tickets")
    op.drop_table("activation_codes")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("is_site_admin")
        batch.drop_column("password_hash")
        batch.drop_column("email")
        batch.alter_column("telegram_id", existing_type=sa.BigInteger(), nullable=False)
