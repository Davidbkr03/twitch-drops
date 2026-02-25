#!/bin/bash
set -e

# Start virtual display for headed Chrome
export DISPLAY=:99
Xvfb :99 -screen 0 1920x1080x24 -ac -nolisten tcp &
sleep 1
echo "Xvfb started on :99"

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

# Ensure new columns exist on older databases
python -c "
import psycopg2, os
conn = psycopg2.connect(os.environ.get('DATABASE_URL', 'postgresql://twitch:twitch@db:5432/twitch_drops'))
cur = conn.cursor()
cur.execute('ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS twitch_username VARCHAR(100)')
cur.execute('ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS twitch_password VARCHAR(256)')
conn.commit()
conn.close()
print('Schema migration OK')
" 2>/dev/null || echo "Schema migration skipped (tables may not exist yet)"

exec "$@"
