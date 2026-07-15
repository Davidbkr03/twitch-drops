import atexit
import os

from app import create_app
from app.config import Config
from app.process_lock import ProcessLock, ProcessLockError


_server_lock = ProcessLock(os.path.join(Config.DATA_DIR, ".server.lock"))
try:
    _server_lock.acquire()
except ProcessLockError as exc:
    raise RuntimeError(str(exc)) from exc
atexit.register(_server_lock.release)

try:
    app = create_app()
except Exception:
    _server_lock.release()
    raise

if __name__ == "__main__":
    raise SystemExit("This service is supported through Docker Compose; see README.md")
