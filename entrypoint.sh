#!/usr/bin/env sh
set -eu

umask 027
mkdir -p "$HOME" "$XDG_CACHE_HOME"

echo "Applying database migrations..."
alembic upgrade head

export DISPLAY=:99
Xvfb "$DISPLAY" -screen 0 1920x1080x24 -ac -nolisten tcp &
xvfb_pid=$!
app_pid=
monitor_pid=

terminate() {
    trap - TERM INT HUP
    if [ -n "$app_pid" ] && kill -0 "$app_pid" 2>/dev/null; then
        kill -TERM "$app_pid" 2>/dev/null || true
    fi
}

cleanup() {
    if [ -n "$monitor_pid" ] && kill -0 "$monitor_pid" 2>/dev/null; then
        kill -TERM "$monitor_pid" 2>/dev/null || true
        wait "$monitor_pid" 2>/dev/null || true
    fi
    if kill -0 "$xvfb_pid" 2>/dev/null; then
        kill -TERM "$xvfb_pid" 2>/dev/null || true
        wait "$xvfb_pid" 2>/dev/null || true
    fi
}

trap terminate TERM INT HUP
trap cleanup EXIT

ready=false
for _attempt in $(seq 1 50); do
    if [ -S /tmp/.X11-unix/X99 ]; then
        ready=true
        break
    fi
    if ! kill -0 "$xvfb_pid" 2>/dev/null; then
        echo "Virtual display exited during startup" >&2
        exit 1
    fi
    sleep 0.1
done
[ "$ready" = true ] || {
    echo "Virtual display did not become ready" >&2
    exit 1
}

echo "Starting application with a supervised virtual display..."
"$@" &
app_pid=$!

monitor_display() {
    while kill -0 "$app_pid" 2>/dev/null; do
        if ! kill -0 "$xvfb_pid" 2>/dev/null; then
            echo "Virtual display exited; stopping the application for container recovery" >&2
            kill -TERM "$app_pid" 2>/dev/null || true
            return 0
        fi
        sleep 2
    done
}
monitor_display &
monitor_pid=$!

set +e
wait "$app_pid"
status=$?
if kill -0 "$app_pid" 2>/dev/null; then
    wait "$app_pid"
    status=$?
fi
set -e
exit "$status"
