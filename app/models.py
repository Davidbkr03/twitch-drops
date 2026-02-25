from datetime import datetime, timezone
from app.extensions import db
from flask_login import UserMixin


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
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False
    )

    # Twitch credentials — stored so the bot can auto-login on start
    twitch_username = db.Column(db.String(100), nullable=True)
    twitch_password = db.Column(db.String(256), nullable=True)

    auto_claim = db.Column(db.Boolean, default=True)
    check_interval = db.Column(db.Integer, default=60)
    screencast_quality = db.Column(db.Integer, default=50)
    screencast_max_fps = db.Column(db.Integer, default=3)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class DropLog(db.Model):
    __tablename__ = "drop_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    drop_name = db.Column(db.String(255))
    game = db.Column(db.String(100))
    status = db.Column(db.String(50))
    progress = db.Column(db.Float, default=0)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    claimed_at = db.Column(db.DateTime, nullable=True)
