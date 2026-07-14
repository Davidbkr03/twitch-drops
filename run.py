import os

from app import create_app
from app.extensions import socketio
from app.process_lock import ProcessLock, ProcessLockError

app = create_app()

if __name__ == "__main__":
    lock_path = os.path.join(app.config["DATA_DIR"], ".server.lock")
    try:
        with ProcessLock(lock_path):
            socketio.run(
                app,
                host="0.0.0.0",
                port=int(os.environ.get("PORT", "5000")),
                debug=False,
                allow_unsafe_werkzeug=True,
            )
    except ProcessLockError as exc:
        raise SystemExit(str(exc)) from None
