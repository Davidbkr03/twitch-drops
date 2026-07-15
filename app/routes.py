import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError

from flask import Blueprint, current_app, render_template, jsonify, request
from flask_login import login_required, current_user
from flask_socketio import join_room, leave_room

from app.extensions import db, socketio
from app.models import UserSettings, DropLog
from app.automator import (
    AutomationManager,
    UserAutomator,
    normalize_twitch_game_url,
)
from app.twitch_pages import normalize_twitch_channel_login

main_bp = Blueprint("main", __name__)
DISCOVERY_TIMEOUT_SECONDS = 75


def _schedule_automator_coroutine(automator, coroutine):
    """Schedule work without leaking a coroutine when its browser loop closes."""
    loop = getattr(automator, "_loop", None)
    if not loop or not loop.is_running():
        close = getattr(coroutine, "close", None)
        if close:
            close()
        return None
    try:
        return asyncio.run_coroutine_threadsafe(coroutine, loop)
    except RuntimeError:
        close = getattr(coroutine, "close", None)
        if close:
            close()
        return None


def _resolve_discovery_future(future, resource_name):
    try:
        return future.result(timeout=DISCOVERY_TIMEOUT_SECONDS), None
    except FutureTimeoutError:
        future.cancel()
        return None, (
            jsonify(
                {
                    "success": False,
                    "error": f"{resource_name} discovery timed out; try again",
                }
            ),
            504,
        )


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


@main_bp.route("/api/import-token", methods=["POST"])
@login_required
def api_import_token():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "JSON object required"}), 400
    raw_token = data.get("auth_token")
    if not isinstance(raw_token, str):
        return jsonify({"success": False, "error": "Token format is invalid"}), 400
    token = raw_token.strip()
    if not token:
        return jsonify({"success": False, "error": "Token required"}), 400
    if len(token) > 512 or not token.isascii():
        return jsonify({"success": False, "error": "Token format is invalid"}), 400
    mgr = AutomationManager.get()
    automator = mgr.get_automator(current_user.id) if mgr else None
    if (
        not automator
        or not automator.context
        or not automator._loop
        or not automator._loop.is_running()
    ):
        return jsonify(
            {
                "success": False,
                "error": "Start automation and wait for the browser before importing a token",
            }
        ), 409
    future = _schedule_automator_coroutine(automator, automator.import_cookies(token))
    if future is None:
        return jsonify({"success": False, "error": "Browser session changed; try again"}), 409
    try:
        imported = future.result(timeout=30)
    except FutureTimeoutError:
        future.cancel()
        return jsonify({"success": False, "error": "Token verification timed out"}), 504
    except Exception:
        current_app.logger.warning("Twitch token import failed", exc_info=True)
        return jsonify({"success": False, "error": "Browser session changed; try again"}), 409
    if not imported:
        return jsonify({"success": False, "error": "Twitch rejected the token"}), 401
    return jsonify({"success": True})


@main_bp.route("/api/discover-games", methods=["POST"])
@login_required
def api_discover_games():
    mgr = AutomationManager.get()
    if not mgr:
        return jsonify({"success": False, "error": "Not ready"}), 503
    a = mgr.get_automator(current_user.id)
    if not a or not a.context:
        return jsonify(
            {"success": False, "error": "Start automation first so the browser is available"}
        ), 400
    try:
        future = _schedule_automator_coroutine(a, UserAutomator.discover_games(a.context))
        if future is None:
            return jsonify({"success": False, "error": "Browser session changed"}), 409
        games, error_response = _resolve_discovery_future(future, "Game")
        if error_response:
            return error_response
        return jsonify({"success": True, "games": games})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@main_bp.route("/api/discover-streamers", methods=["POST"])
@login_required
def api_discover_streamers():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "JSON object required"}), 400
    game_url = data.get("game_url", "")
    if not game_url:
        return jsonify({"success": False, "error": "game_url required"}), 400
    try:
        game_url = normalize_twitch_game_url(game_url)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    mgr = AutomationManager.get()
    if not mgr:
        return jsonify({"success": False, "error": "Not ready"}), 503
    a = mgr.get_automator(current_user.id)
    if not a or not a.context:
        return jsonify({"success": False, "error": "Start automation first"}), 400
    try:
        future = _schedule_automator_coroutine(
            a, UserAutomator.discover_streamers(a.context, game_url)
        )
        if future is None:
            return jsonify({"success": False, "error": "Browser session changed"}), 409
        streamers, error_response = _resolve_discovery_future(future, "Streamer")
        if error_response:
            return error_response
        return jsonify({"success": True, "streamers": streamers})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@main_bp.route("/api/watch-targets", methods=["GET", "POST", "DELETE"])
@login_required
def api_watch_targets():
    from app.models import WatchTarget

    if request.method == "GET":
        rows = WatchTarget.query.filter_by(user_id=current_user.id).all()
        return jsonify(
            [
                {
                    "id": r.id,
                    "game_name": r.game_name,
                    "game_url": r.game_url,
                    "streamer": r.streamer,
                    "enabled": r.enabled,
                }
                for r in rows
            ]
        )

    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "JSON object required"}), 400
        raw_game_name = data.get("game_name") or ""
        if not isinstance(raw_game_name, str):
            return jsonify({"success": False, "error": "game_name must be a string"}), 400
        game_name = raw_game_name.strip()
        if not game_name:
            return jsonify({"success": False, "error": "game_name required"}), 400
        raw_game_url = data.get("game_url") or ""
        try:
            game_url = normalize_twitch_game_url(raw_game_url)
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        raw_streamer = data.get("streamer") or ""
        if not isinstance(raw_streamer, str):
            return jsonify({"success": False, "error": "streamer must be a string"}), 400
        try:
            streamer = (
                normalize_twitch_channel_login(raw_streamer) if raw_streamer.strip() else None
            )
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        existing = WatchTarget.query.filter_by(
            user_id=current_user.id,
            game_url=game_url,
            streamer=streamer,
        ).first()
        if existing:
            return jsonify({"success": True, "id": existing.id, "existing": True})
        wt = WatchTarget(
            user_id=current_user.id,
            game_name=game_name,
            game_url=game_url,
            streamer=streamer,
        )
        db.session.add(wt)
        db.session.commit()
        return jsonify({"success": True, "id": wt.id})

    if request.method == "DELETE":
        data = request.get_json(silent=True) or {}
        tid = data.get("id")
        if tid:
            WatchTarget.query.filter_by(id=tid, user_id=current_user.id).delete()
            db.session.commit()
        return jsonify({"success": True})


@main_bp.route("/api/settings", methods=["GET", "POST"])
@login_required
def api_settings():
    s = UserSettings.query.filter_by(user_id=current_user.id).first()
    if not s:
        s = UserSettings(user_id=current_user.id)
        db.session.add(s)
        db.session.commit()

    if request.method == "GET":
        return jsonify(
            {
                "auto_claim": s.auto_claim,
                "check_interval": s.check_interval,
                "screencast_quality": s.screencast_quality,
                "screencast_max_fps": s.screencast_max_fps,
            }
        )

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "JSON object required"}), 400
    if "auto_claim" in data:
        if not isinstance(data["auto_claim"], bool):
            return jsonify({"success": False, "error": "auto_claim must be a boolean"}), 400
        s.auto_claim = data["auto_claim"]

    integer_settings = {
        "check_interval": (10, 600),
        "screencast_quality": (10, 100),
        "screencast_max_fps": (1, 10),
    }
    for name, (minimum, maximum) in integer_settings.items():
        if name not in data:
            continue
        value = data[name]
        if isinstance(value, bool) or not isinstance(value, int):
            return jsonify({"success": False, "error": f"{name} must be an integer"}), 400
        if not minimum <= value <= maximum:
            return jsonify(
                {
                    "success": False,
                    "error": f"{name} must be between {minimum} and {maximum}",
                }
            ), 400
        setattr(s, name, value)
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
# Socket.IO
# ------------------------------------------------------------------


@socketio.on("connect")
def on_connect():
    if not current_user.is_authenticated:
        return False
    join_room(f"user_{current_user.id}")
    mgr = AutomationManager.get()
    if mgr:
        mgr.set_preview_connected(current_user.id, True)
        socketio.emit(
            "automation_status",
            mgr.get_status(current_user.id),
            room=f"user_{current_user.id}",
        )


@socketio.on("disconnect")
def on_disconnect():
    if current_user.is_authenticated:
        leave_room(f"user_{current_user.id}")
        mgr = AutomationManager.get()
        if mgr:
            mgr.set_preview_connected(current_user.id, False)


@socketio.on("browser_input")
def on_browser_input(data):
    if not current_user.is_authenticated:
        return
    mgr = AutomationManager.get()
    if not mgr:
        return
    a = mgr.get_automator(current_user.id)
    if a and a._loop and a._loop.is_running():
        _schedule_automator_coroutine(a, a.handle_input(data))
