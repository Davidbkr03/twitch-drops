from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user

from app.extensions import db, bcrypt
from app.models import User, UserSettings

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and bcrypt.check_password_hash(user.password_hash, password):
            login_user(user, remember=True)
            next_page = request.args.get("next")
            return redirect(next_page or url_for("main.dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if not username or not password:
            flash("Username and password are required.", "error")
        elif len(username) < 3:
            flash("Username must be at least 3 characters.", "error")
        elif len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
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
            return redirect(url_for("main.dashboard"))
    return render_template("register.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
