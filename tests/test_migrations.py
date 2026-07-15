import os
from pathlib import Path
import sqlite3
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def _upgrade(database_path: Path, data_dir: Path) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "DATA_DIR": str(data_dir),
            "DATABASE_URL": f"sqlite:///{database_path.resolve().as_posix()}",
            "SECRET_KEY": "migration-test-secret",
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def test_fresh_database_upgrade_creates_frozen_production_schema(tmp_path):
    database_path = tmp_path / "fresh.db"

    _upgrade(database_path, tmp_path / "data")

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {
            "alembic_version",
            "users",
            "user_settings",
            "watch_targets",
            "drop_logs",
        } <= tables
        assert "automation_enabled" in _columns(connection, "user_settings")
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "20260715_01",
        )
        drop_indexes = {row[1] for row in connection.execute('PRAGMA index_list("drop_logs")')}
        assert "ix_drop_logs_user_created" in drop_indexes


def test_legacy_database_upgrade_preserves_data_and_removes_credentials(tmp_path):
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username VARCHAR(80) NOT NULL UNIQUE,
                password_hash VARCHAR(256) NOT NULL,
                created_at DATETIME,
                is_active BOOLEAN
            );
            CREATE TABLE user_settings (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
                twitch_username VARCHAR(100),
                twitch_password VARCHAR(256),
                twitch_auth_token TEXT,
                auto_claim BOOLEAN,
                check_interval INTEGER,
                screencast_quality INTEGER,
                screencast_max_fps INTEGER,
                updated_at DATETIME
            );
            INSERT INTO users (id, username, password_hash, is_active)
                VALUES (1, 'owner', 'hash', 1);
            INSERT INTO user_settings (
                id, user_id, twitch_username, twitch_password, twitch_auth_token,
                auto_claim, check_interval, screencast_quality, screencast_max_fps
            ) VALUES (1, 1, 'legacy-user', 'legacy-password', 'legacy-token', 1, 60, 50, 3);
            """
        )

    _upgrade(database_path, tmp_path / "data")

    with sqlite3.connect(database_path) as connection:
        columns = _columns(connection, "user_settings")
        assert "automation_enabled" in columns
        assert "twitch_username" not in columns
        assert "twitch_password" not in columns
        assert "twitch_auth_token" not in columns
        assert connection.execute("SELECT username FROM users WHERE id = 1").fetchone() == (
            "owner",
        )
        assert connection.execute(
            "SELECT automation_enabled FROM user_settings WHERE user_id = 1"
        ).fetchone() == (0,)
