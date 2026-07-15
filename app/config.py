import os
import secrets


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_or_create_secret_key(data_dir: str) -> str:
    """Return a stable local secret without shipping a forgeable default."""
    configured = os.environ.get("SECRET_KEY")
    if configured:
        return configured

    os.makedirs(data_dir, exist_ok=True)
    key_path = os.path.join(data_dir, ".secret_key")
    try:
        with open(key_path, "r", encoding="utf-8") as key_file:
            existing = key_file.read().strip()
            if existing:
                return existing
    except FileNotFoundError:
        pass

    generated = secrets.token_urlsafe(48)
    try:
        descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        with open(key_path, "r", encoding="utf-8") as key_file:
            existing = key_file.read().strip()
            if existing:
                return existing
        raise RuntimeError(f"Secret key file is empty: {key_path}")

    with os.fdopen(descriptor, "w", encoding="utf-8") as key_file:
        key_file.write(generated)
    return generated


DEFAULT_DATA_DIR = os.path.join(PROJECT_ROOT, ".runtime")
CONFIG_DATA_DIR = os.path.abspath(os.environ.get("DATA_DIR", DEFAULT_DATA_DIR))


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    DATA_DIR = CONFIG_DATA_DIR
    SECRET_KEY = _load_or_create_secret_key(DATA_DIR)
    BOOTSTRAP_TOKEN = os.environ.get("BOOTSTRAP_TOKEN", "")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        f"sqlite:///{os.path.join(DATA_DIR, 'twitch_drops.db')}",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }
    BROWSER_DATA_DIR = os.path.join(DATA_DIR, "browser")
    ALLOW_REGISTRATION = _env_bool("ALLOW_REGISTRATION", False)
    AUTO_RESUME_ENABLED = _env_bool("AUTO_RESUME_ENABLED", True)
    AUTOMATION_RECONCILE_INTERVAL_SECONDS = max(
        5, int(os.environ.get("AUTOMATION_RECONCILE_INTERVAL_SECONDS", "30"))
    )
    MAX_AUTOMATORS = max(1, int(os.environ.get("MAX_AUTOMATORS", "2")))
    AUTOMATION_RETRY_BASE_SECONDS = max(
        1, int(os.environ.get("AUTOMATION_RETRY_BASE_SECONDS", "5"))
    )
    AUTOMATION_RETRY_MAX_SECONDS = max(
        AUTOMATION_RETRY_BASE_SECONDS,
        int(os.environ.get("AUTOMATION_RETRY_MAX_SECONDS", "300")),
    )
    DROP_LOG_RETENTION_DAYS = max(30, int(os.environ.get("DROP_LOG_RETENTION_DAYS", "365")))
    DROP_LOG_MAX_ROWS_PER_USER = max(
        1000, int(os.environ.get("DROP_LOG_MAX_ROWS_PER_USER", "10000"))
    )
    MAX_CONTENT_LENGTH = 64 * 1024
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Strict"
    SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", False)
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Strict"
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    WTF_CSRF_TIME_LIMIT = None
    RATELIMIT_ENABLED = _env_bool("RATELIMIT_ENABLED", True)
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
