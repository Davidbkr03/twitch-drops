import asyncio
from datetime import datetime, timezone

from flask import Blueprint, render_template, jsonify, request
from flask_login import login_required, current_user
from flask_socketio import join_room, leave_room

from app.extensions import db, socketio
from app.models import UserSettings, DropLog
from app.automator import AutomationManager

main_bp = Blueprint("main", __name__)


# ------------------------------------------------------------------
# Pages
# ------------------------------------------------------------------


@main_bp.route("/")
@login_required
def dashboard():
    settings = UserSettings.query.filter_by(user_id=current_user.id).first()
    return render_template("dashboard.html", settings=settings)


# ------------------------------------------------------------------
# REST API
# ------------------------------------------------------------------


@main_bp.route("/api/status")
@login_required
def api_status():
    mgr = AutomationManager.get()
    status = mgr.get_status(current_user.id) if mgr else {"running": False}
    return jsonify(status)


@main_bp.route("/api/start", methods=["POST"])
@login_required
def api_start():
    mgr = AutomationManager.get()
    if not mgr:
        return jsonify({"success": False, "error": "Manager not ready"}), 503
    ok = mgr.start_for_user(current_user.id)
    return jsonify({"success": ok})


@main_bp.route("/api/stop", methods=["POST"])
@login_required
def api_stop():
    mgr = AutomationManager.get()
    if not mgr:
        return jsonify({"success": False, "error": "Manager not ready"}), 503
    ok = mgr.stop_for_user(current_user.id)
    return jsonify({"success": ok})


@main_bp.route("/api/settings", methods=["GET", "POST"])
@login_required
def api_settings():
    settings = UserSettings.query.filter_by(user_id=current_user.id).first()
    if not settings:
        settings = UserSettings(user_id=current_user.id)
        db.session.add(settings)
        db.session.commit()

    if request.method == "GET":
        return jsonify(
            {
                "auto_claim": settings.auto_claim,
                "check_interval": settings.check_interval,
                "screencast_quality": settings.screencast_quality,
                "screencast_max_fps": settings.screencast_max_fps,
            }
        )

    data = request.get_json(silent=True) or {}
    if "auto_claim" in data:
        settings.auto_claim = bool(data["auto_claim"])
    if "check_interval" in data:
        settings.check_interval = max(10, int(data["check_interval"]))
    if "screencast_quality" in data:
        settings.screencast_quality = max(10, min(100, int(data["screencast_quality"])))
    if "screencast_max_fps" in data:
        settings.screencast_max_fps = max(1, min(10, int(data["screencast_max_fps"])))
    db.session.commit()
    return jsonify({"success": True})


@main_bp.route("/api/drops")
@login_required
def api_drops():
    logs = (
        DropLog.query.filter_by(user_id=current_user.id)
        .order_by(DropLog.created_at.desc())
        .limit(100)
        .all()
    )
    return jsonify(
        [
            {
                "id": d.id,
                "name": d.drop_name,
                "game": d.game,
                "status": d.status,
                "progress": d.progress,
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "claimed_at": d.claimed_at.isoformat() if d.claimed_at else None,
            }
            for d in logs
        ]
    )


# ------------------------------------------------------------------
# Socket.IO events
# ------------------------------------------------------------------


@socketio.on("connect")
def on_connect():
    if current_user.is_authenticated:
        join_room(f"user_{current_user.id}")
        mgr = AutomationManager.get()
        if mgr:
            status = mgr.get_status(current_user.id)
            socketio.emit(
                "automation_status", status, room=f"user_{current_user.id}"
            )


@socketio.on("disconnect")
def on_disconnect():
    if current_user.is_authenticated:
        leave_room(f"user_{current_user.id}")


@socketio.on("browser_input")
def on_browser_input(data):
    if not current_user.is_authenticated:
        return
    mgr = AutomationManager.get()
    if not mgr:
        return
    automator = mgr.get_automator(current_user.id)
    if automator and automator._loop and automator._loop.is_running():
        asyncio.run_coroutine_threadsafe(
            automator.handle_input(data), automator._loop
        )


@socketio.on("twitch_login")
def on_twitch_login(data):
    if not current_user.is_authenticated:
        return
    mgr = AutomationManager.get()
    if not mgr:
        return
    automator = mgr.get_automator(current_user.id)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return
    if automator and automator._loop and automator._loop.is_running():
        asyncio.run_coroutine_threadsafe(
            automator.auto_login(username, password), automator._loop
        )
