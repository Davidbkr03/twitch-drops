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


class Config:
    DATA_DIR = CONFIG_DATA_DIR
    SECRET_KEY = _load_or_create_secret_key(DATA_DIR)
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        f"sqlite:///{os.path.join(DATA_DIR, 'twitch_drops.db')}",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    BROWSER_DATA_DIR = os.path.join(DATA_DIR, "browser")
    NATIVE_LOGIN_ENABLED = (
        os.name == "nt"
        and os.environ.get("NATIVE_LOGIN_ENABLED", "1").lower() not in {"0", "false", "no"}
    )
