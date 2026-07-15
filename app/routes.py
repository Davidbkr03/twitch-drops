import asyncio
import ipaddress
from concurrent.futures import TimeoutError as FutureTimeoutError
from urllib.parse import urlsplit

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


def _resolve_discovery_future(future, resource_name):
    try:
        return future.result(timeout=DISCOVERY_TIMEOUT_SECONDS), None
    except FutureTimeoutError:
        future.cancel()
        return None, (
            jsonify({
                "success": False,
                "error": f"{resource_name} discovery timed out; try again",
            }),
            504,
        )


def _is_local_same_origin_request() -> bool:
    """Restrict native desktop launches to the machine hosting the app."""
    try:
        remote = ipaddress.ip_address(request.remote_addr or "")
    except ValueError:
        return False
    is_loopback = remote.is_loopback or bool(
        getattr(remote, "ipv4_mapped", None) and remote.ipv4_mapped.is_loopback
    )
    if not is_loopback:
        return False

    origin = request.headers.get("Origin")
    if not origin:
        return False
    supplied = urlsplit(origin)
    expected = urlsplit(request.host_url)
    return (
        supplied.scheme.lower(),
        supplied.hostname,
        supplied.port,
    ) == (
        expected.scheme.lower(),
        expected.hostname,
        expected.port,
    )


# ------------------------------------------------------------------
# Pages
# ------------------------------------------------------------------

@main_bp.route("/")
@login_required
def dashboard():
    settings = UserSettings.query.filter_by(user_id=current_user.id).first()
    return render_template(
        "dashboard.html",
        settings=settings,
        native_login_enabled=current_app.config.get("NATIVE_LOGIN_ENABLED", False),
    )


# ------------------------------------------------------------------
# REST API
# ------------------------------------------------------------------

@main_bp.route("/api/status")
@login_required
def api_status():
    mgr = AutomationManager.get()
    status = mgr.get_status(current_user.id) if mgr else {"running": False}
    # Supplement with DB info
    s = UserSettings.query.filter_by(user_id=current_user.id).first()
    if s and (s.twitch_username or s.twitch_auth_token):
        status["twitch_user"] = s.twitch_username
        status["twitch_saved"] = True
    return jsonify(status)


@main_bp.route("/api/start", methods=["POST"])
@login_required
def api_start():
    mgr = AutomationManager.get()
    if not mgr:
        return jsonify({"success": False, "error": "Manager not ready"}), 503
    ok = mgr.start_for_user(current_user.id)
    if not ok and mgr.native_login_active_for_user(current_user.id):
        return jsonify({
            "success": False,
            "error": "Close the normal Twitch login browser before starting",
        }), 409
    return jsonify({"success": ok})


@main_bp.route("/api/stop", methods=["POST"])
@login_required
def api_stop():
    mgr = AutomationManager.get()
    if not mgr:
        return jsonify({"success": False, "error": "Manager not ready"}), 503
    ok = mgr.stop_for_user(current_user.id)
    return jsonify({"success": ok})


@main_bp.route("/api/twitch-account", methods=["GET", "POST"])
@login_required
def api_twitch_account():
    s = UserSettings.query.filter_by(user_id=current_user.id).first()
    if not s:
        s = UserSettings(user_id=current_user.id)
        db.session.add(s)
        db.session.commit()

    if request.method == "GET":
        return jsonify({
            "twitch_username": s.twitch_username or "",
            "has_password": bool(s.twitch_password),
        })

    data = request.get_json(silent=True) or {}
    u = (data.get("twitch_username") or "").strip()
    p = data.get("twitch_password") or ""
    if not u:
        return jsonify({"success": False, "error": "Username required"}), 400
    s.twitch_username = u
    if p:
        s.twitch_password = p
    db.session.commit()
    return jsonify({"success": True})


@main_bp.route("/api/import-token", methods=["POST"])
@login_required
def api_import_token():
    data = request.get_json(silent=True) or {}
    token = (data.get("auth_token") or "").strip()
    if not token:
        return jsonify({"success": False, "error": "Token required"}), 400
    s = UserSettings.query.filter_by(user_id=current_user.id).first()
    if not s:
        s = UserSettings(user_id=current_user.id)
        db.session.add(s)
    s.twitch_auth_token = token
    db.session.commit()
    # If automation is running, apply immediately
    mgr = AutomationManager.get()
    if mgr:
        a = mgr.get_automator(current_user.id)
        if a and a._loop and a._loop.is_running():
            import asyncio
            asyncio.run_coroutine_threadsafe(a.import_cookies(token), a._loop)
    return jsonify({"success": True})


@main_bp.route("/api/native-login", methods=["POST"])
@login_required
def api_native_login():
    if not current_app.config.get("NATIVE_LOGIN_ENABLED", False):
        return jsonify({
            "success": False,
            "error": "Native login is only available on a local desktop install",
        }), 409
    if not _is_local_same_origin_request():
        return jsonify({"success": False, "error": "Local same-origin request required"}), 403
    mgr = AutomationManager.get()
    if not mgr:
        return jsonify({"success": False, "error": "Manager not ready"}), 503
    success, detail = mgr.open_native_login_for_user(current_user.id)
    if not success:
        return jsonify({"success": False, "error": detail}), 409
    return jsonify({"success": True, "browser": detail})


@main_bp.route("/api/discover-games", methods=["POST"])
@login_required
def api_discover_games():
    mgr = AutomationManager.get()
    if not mgr:
        return jsonify({"success": False, "error": "Not ready"}), 503
    a = mgr.get_automator(current_user.id)
    if not a or not a.context:
        return jsonify({"success": False, "error": "Start automation first so the browser is available"}), 400
    import asyncio
    try:
        future = asyncio.run_coroutine_threadsafe(
            UserAutomator.discover_games(a.context), a._loop
        )
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
    import asyncio
    try:
        future = asyncio.run_coroutine_threadsafe(
            UserAutomator.discover_streamers(a.context, game_url), a._loop
        )
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
        return jsonify([{
            "id": r.id, "game_name": r.game_name, "game_url": r.game_url,
            "streamer": r.streamer, "enabled": r.enabled,
        } for r in rows])

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
                normalize_twitch_channel_login(raw_streamer)
                if raw_streamer.strip()
                else None
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
        return jsonify({
            "auto_claim": s.auto_claim,
            "check_interval": s.check_interval,
            "screencast_quality": s.screencast_quality,
            "screencast_max_fps": s.screencast_max_fps,
        })

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
            return jsonify({
                "success": False,
                "error": f"{name} must be between {minimum} and {maximum}",
            }), 400
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
    return jsonify([{
        "id": d.id, "name": d.drop_name, "game": d.game,
        "status": d.status, "progress": d.progress,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "claimed_at": d.claimed_at.isoformat() if d.claimed_at else None,
    } for d in logs])


# ------------------------------------------------------------------
# Socket.IO
# ------------------------------------------------------------------

@socketio.on("connect")
def on_connect():
    if current_user.is_authenticated:
        join_room(f"user_{current_user.id}")
        mgr = AutomationManager.get()
        if mgr:
            socketio.emit("automation_status", mgr.get_status(current_user.id), room=f"user_{current_user.id}")


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
    a = mgr.get_automator(current_user.id)
    if a and a._loop and a._loop.is_running():
        asyncio.run_coroutine_threadsafe(a.handle_input(data), a._loop)


@socketio.on("twitch_login")
def on_twitch_login(data):
    if not current_user.is_authenticated:
        return
    mgr = AutomationManager.get()
    if not mgr:
        return
    a = mgr.get_automator(current_user.id)
    u = (data.get("username") or "").strip()
    p = data.get("password") or ""
    if not u or not p:
        return
    if a and a._loop and a._loop.is_running():
        asyncio.run_coroutine_threadsafe(a.auto_login(u, p), a._loop)
