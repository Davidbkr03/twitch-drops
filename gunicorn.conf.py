import os


bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:5000")
workers = 1
worker_class = "gthread"
threads = int(os.environ.get("GUNICORN_THREADS", "8"))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "50"))
keepalive = 5
accesslog = "-"
errorlog = "-"
capture_output = True
worker_tmp_dir = "/dev/shm"


def worker_exit(server, worker):
    from app.automator import AutomationManager

    manager = AutomationManager.get()
    if manager:
        manager.shutdown(timeout=max(1, graceful_timeout - 5))
