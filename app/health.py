import os

from flask import Blueprint, current_app, jsonify
from sqlalchemy import select, text

from app.automator import AutomationManager
from app.extensions import db
from app.models import UserSettings


health_bp = Blueprint("health", __name__)


@health_bp.after_request
def disable_health_caching(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@health_bp.get("/health/live")
def live():
    return jsonify({"status": "ok"})


@health_bp.get("/health/ready")
def ready():
    try:
        db.session.execute(text("SELECT 1"))
        manager = AutomationManager.get()
        if manager is None or manager.is_shutting_down():
            raise RuntimeError("automation manager is unavailable")
        if (
            current_app.config.get("AUTO_RESUME_ENABLED", False)
            and not manager.reconciler_is_alive()
        ):
            raise RuntimeError("automation reconciler is unavailable")
        desired_user_ids = db.session.scalars(
            select(UserSettings.user_id).where(UserSettings.automation_enabled.is_(True))
        )
        if any(
            not (automator := manager.get_automator(user_id)) or not automator.is_alive()
            for user_id in desired_user_ids
        ):
            raise RuntimeError("an enabled automation worker is unavailable")

        display = os.environ.get("DISPLAY", "")
        if display.startswith(":"):
            display_number = display[1:].split(".", 1)[0]
            if display_number.isdigit() and not os.path.exists(f"/tmp/.X11-unix/X{display_number}"):
                raise RuntimeError("virtual display is unavailable")
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"status": "unavailable"}), 503
    return jsonify({"status": "ok"})
