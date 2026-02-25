#!/bin/bash
set -e

echo "Waiting for database..."
for i in $(seq 1 30); do
    if python -c "
import psycopg2, os
try:
    conn = psycopg2.connect(os.environ.get('DATABASE_URL', 'postgresql://twitch:twitch@db:5432/twitch_drops'))
    conn.close()
    exit(0)
except Exception:
    exit(1)
" 2>/dev/null; then
        echo "Database is ready."
        break
    fi
    echo "  waiting... ($i/30)"
    sleep 2
done

exec "$@"
