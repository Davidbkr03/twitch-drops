import atexit
import logging
import os
from logging.config import dictConfig

from flask import Flask, jsonify, request
from flask_wtf.csrf import CSRFError

from app.config import Config
from app.extensions import bcrypt, csrf, db, limiter, login_manager, socketio


def _configure_logging(app: Flask) -> None:
    if app.testing:
        return
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                }
            },
            "root": {
                "handlers": ["console"],
                "level": os.environ.get("LOG_LEVEL", "INFO").upper(),
            },
        }
    )
    logging.captureWarnings(True)


def create_app(config_class=Config):
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app = Flask(
        __name__,
        template_folder=os.path.join(base, "templates"),
        static_folder=os.path.join(base, "static"),
    )
    app.config.from_object(config_class)
    _configure_logging(app)

    db.init_app(app)
    login_manager.init_app(app)
    bcrypt.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)
    socketio.init_app(
        app,
        cors_allowed_origins=None,
        async_mode="threading",
        max_http_buffer_size=1_000_000,
    )

    login_manager.login_view = "auth.login"

    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    from app.auth import auth_bp
    from app.health import health_bp
    from app.routes import main_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(main_bp)

    if app.testing:
        with app.app_context():
            db.create_all()

    @app.errorhandler(CSRFError)
    def handle_csrf_error(error):
        if request.path.startswith("/api/"):
            return jsonify({"success": False, "error": "Request verification failed"}), 400
        return error.description, 400

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; "
            "connect-src 'self' ws: wss:; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        return response

    from app.automator import AutomationManager

    manager = AutomationManager.init(socketio, app)
    if app.config.get("AUTO_RESUME_ENABLED", False):
        manager.restore_enabled_users()
        manager.start_reconciler()
    if not app.testing:
        atexit.register(manager.shutdown)

    return app
