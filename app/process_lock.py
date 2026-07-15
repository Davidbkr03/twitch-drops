"""Cross-platform process lock for a single server per data directory."""

import os


class ProcessLockError(RuntimeError):
    """Raised when another server process already owns the data directory."""


class ProcessLock:
    def __init__(self, path: str):
        self.path = path
        self._file = None

    def __enter__(self):
        return self.acquire()

    def acquire(self):
        if self._file:
            return self
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        lock_file = open(self.path, "a+b")
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock_file.close()
            raise ProcessLockError(
                f"Another server is already using data directory {os.path.dirname(self.path)}"
            ) from exc
        self._file = lock_file
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()

    def release(self):
        if not self._file:
            return
        try:
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None
