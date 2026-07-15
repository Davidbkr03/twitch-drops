from datetime import datetime, timezone
from flask_login import UserMixin
from sqlalchemy import false

from app.extensions import db


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    is_active = db.Column(db.Boolean, default=True)

    settings = db.relationship(
        "UserSettings", backref="user", uselist=False, cascade="all,delete-orphan"
    )
    drop_logs = db.relationship(
        "DropLog", backref="user", cascade="all,delete-orphan", lazy="dynamic"
    )


class UserSettings(db.Model):
    __tablename__ = "user_settings"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)

    # Twitch session credentials live only in the persistent browser profile.
    automation_enabled = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    auto_claim = db.Column(db.Boolean, default=True)
    check_interval = db.Column(db.Integer, default=60)
    screencast_quality = db.Column(db.Integer, default=50)
    screencast_max_fps = db.Column(db.Integer, default=3)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class WatchTarget(db.Model):
    """Games + optional specific streamers a user wants to watch for drops."""

    __tablename__ = "watch_targets"
    __table_args__ = (db.Index("ix_watch_targets_user_enabled", "user_id", "enabled"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    game_name = db.Column(db.String(200), nullable=False)
    game_url = db.Column(db.String(500), nullable=True)
    streamer = db.Column(db.String(100), nullable=True)
    enabled = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class DropLog(db.Model):
    __tablename__ = "drop_logs"
    __table_args__ = (db.Index("ix_drop_logs_user_created", "user_id", "created_at"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    drop_name = db.Column(db.String(255))
    game = db.Column(db.String(100))
    status = db.Column(db.String(50))
    progress = db.Column(db.Float, default=0)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    claimed_at = db.Column(db.DateTime, nullable=True)
