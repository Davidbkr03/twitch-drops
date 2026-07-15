import secrets
import threading
from urllib.parse import unquote, urlsplit

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import login_user, logout_user, login_required, current_user

from app.extensions import bcrypt, db, limiter
from app.models import User, UserSettings

auth_bp = Blueprint("auth", __name__)
_registration_lock = threading.Lock()


def _registration_is_open() -> bool:
    return (
        bool(current_app.config.get("ALLOW_REGISTRATION", False))
        or db.session.query(User.id).first() is None
    )


def _safe_local_redirect(target):
    """Accept only absolute-path redirects on this application."""
    if not target:
        return None
    decoded = unquote(target)
    if "\\" in decoded or any(
        ord(character) < 32 or ord(character) == 127 for character in decoded
    ):
        return None
    parsed = urlsplit(decoded)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return None
    if decoded.startswith("//"):
        return None
    return target


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and bcrypt.check_password_hash(user.password_hash, password):
            login_user(user, remember=True)
            next_page = _safe_local_redirect(request.args.get("next"))
            return redirect(next_page or url_for("main.dashboard"), code=303)
        flash("Invalid username or password.", "error")
    return render_template("login.html", registration_open=_registration_is_open())


@auth_bp.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    if request.method == "GET" and not _registration_is_open():
        flash("Registration is closed. Sign in with the owner account.", "error")
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        # Gunicorn uses threads, so two first-time requests can otherwise both
        # pass the empty-database check before either transaction commits.
        with _registration_lock:
            if not _registration_is_open():
                flash("Registration is closed. Sign in with the owner account.", "error")
                return redirect(url_for("auth.login"), code=303)

            expected_bootstrap_token = current_app.config.get("BOOTSTRAP_TOKEN", "")
            supplied_bootstrap_token = request.form.get("bootstrap_token", "")
            if expected_bootstrap_token and not secrets.compare_digest(
                supplied_bootstrap_token, expected_bootstrap_token
            ):
                flash("The deployment bootstrap token is invalid.", "error")
                return render_template("register.html", bootstrap_token_required=True), 403

            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirm = request.form.get("confirm_password", "")

            if not username or not password:
                flash("Username and password are required.", "error")
            elif len(username) < 3:
                flash("Username must be at least 3 characters.", "error")
            elif len(password) < 12:
                flash("Password must be at least 12 characters.", "error")
            elif password != confirm:
                flash("Passwords do not match.", "error")
            elif User.query.filter_by(username=username).first():
                flash("Username already taken.", "error")
            else:
                pw_hash = bcrypt.generate_password_hash(password).decode("utf-8")
                user = User(username=username, password_hash=pw_hash)
                db.session.add(user)
                db.session.flush()
                settings = UserSettings(user_id=user.id)
                db.session.add(settings)
                db.session.commit()
                login_user(user, remember=True)
                return redirect(url_for("main.dashboard"), code=303)
    return render_template(
        "register.html",
        bootstrap_token_required=bool(current_app.config.get("BOOTSTRAP_TOKEN")),
    )


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
