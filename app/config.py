import os


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        "postgresql://twitch:twitch@db:5432/twitch_drops",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    DATA_DIR = os.environ.get("DATA_DIR", "/data")
    BROWSER_DATA_DIR = os.path.join(DATA_DIR, "browser")
