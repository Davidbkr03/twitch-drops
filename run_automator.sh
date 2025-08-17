#!/usr/bin/env bash

# Change to the directory where this script resides
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Prefer venv interpreter if available, else fall back to system python3
if [ -x "./venv/bin/python" ]; then
    ./venv/bin/python twitch_drop_automator.py &>/dev/null &
else
    if command -v python3 >/dev/null 2>&1; then
        python3 twitch_drop_automator.py &>/dev/null &
    else
        echo "python3 not found. Please install Python 3 and create a venv (see README)." >&2
        exit 1
    fi
fi

echo "Twitch Drop Automator started. Check drops_log.txt for logs."

