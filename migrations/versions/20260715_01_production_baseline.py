"""Create the production schema and remove stored Twitch credentials.

Revision ID: 20260715_01
Revises:
Create Date: 2026-07-15
"""

from alembic import op
import sqlalchemy as sa

revision = "20260715_01"
down_revision = None
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _index_names(table_name: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}


def _create_missing_tables() -> None:
    """Create a frozen baseline without depending on future model changes."""
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())

    if "users" not in existing:
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("username", sa.String(length=80), nullable=False),
            sa.Column("password_hash", sa.String(length=256), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=True),
            sa.UniqueConstraint("username"),
        )
    if "user_settings" not in existing:
        op.create_table(
            "user_settings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column(
                "automation_enabled",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
            sa.Column("auto_claim", sa.Boolean(), nullable=True),
            sa.Column("check_interval", sa.Integer(), nullable=True),
            sa.Column("screencast_quality", sa.Integer(), nullable=True),
            sa.Column("screencast_max_fps", sa.Integer(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.UniqueConstraint("user_id"),
        )
    if "watch_targets" not in existing:
        op.create_table(
            "watch_targets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("game_name", sa.String(length=200), nullable=False),
            sa.Column("game_url", sa.String(length=500), nullable=True),
            sa.Column("streamer", sa.String(length=100), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        )
    if "drop_logs" not in existing:
        op.create_table(
            "drop_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("drop_name", sa.String(length=255), nullable=True),
            sa.Column("game", sa.String(length=100), nullable=True),
            sa.Column("status", sa.String(length=50), nullable=True),
            sa.Column("progress", sa.Float(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("claimed_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        )


def upgrade() -> None:
    # This is both the initial schema and the adoption migration for databases
    # created by older releases that used db.create_all() without Alembic.
    _create_missing_tables()

    columns = _column_names("user_settings")
    with op.batch_alter_table("user_settings") as batch_op:
        if "automation_enabled" not in columns:
            batch_op.add_column(
                sa.Column(
                    "automation_enabled",
                    sa.Boolean(),
                    server_default=sa.false(),
                    nullable=False,
                )
            )

        # Passwords and imported auth tokens must never remain in the database.
        for credential_column in (
            "twitch_password",
            "twitch_auth_token",
            "twitch_username",
        ):
            if credential_column in columns:
                batch_op.drop_column(credential_column)

    if "ix_watch_targets_user_enabled" not in _index_names("watch_targets"):
        op.create_index(
            "ix_watch_targets_user_enabled",
            "watch_targets",
            ["user_id", "enabled"],
        )
    if "ix_drop_logs_user_created" not in _index_names("drop_logs"):
        op.create_index(
            "ix_drop_logs_user_created",
            "drop_logs",
            ["user_id", "created_at"],
        )


def downgrade() -> None:
    raise RuntimeError(
        "Database downgrade is intentionally unsupported; restore the matching "
        "pre-update backup instead"
    )
