#!/usr/bin/env bash
set -Eeuo pipefail

# shellcheck source=lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
acquire_operation_lock

umask 077

CONFIGURED_BACKUP_ROOT="$(env_value BACKUP_ROOT)"
BACKUP_ROOT="${1:-${CONFIGURED_BACKUP_ROOT:-$ROOT_DIR/backups}}"
[[ "$BACKUP_ROOT" == /* ]] || die "backup root must be an absolute path"
mkdir -p "$BACKUP_ROOT"
BACKUP_ROOT="$(cd -- "$BACKUP_ROOT" && pwd -P)"
[[ "$BACKUP_ROOT" != "/" ]] || die "refusing to use the filesystem root as a backup destination"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
[[ "$RETENTION_DAYS" =~ ^[0-9]+$ \
    && "$RETENTION_DAYS" -ge 7 \
    && "$RETENTION_DAYS" -le 3650 ]] \
    || die "BACKUP_RETENTION_DAYS must be between 7 and 3650"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="$BACKUP_ROOT/$TIMESTAMP"
mkdir -p "$BACKUP_DIR"
[[ -z "$(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]] \
    || die "backup destination is not empty: $BACKUP_DIR"

APP_WAS_RUNNING=false
APP_STOPPED=false
# Default to preserving the database. Only mark it stopped after Compose state
# has been read successfully, so an early config/daemon error cannot stop it.
DB_WAS_RUNNING=true

prune_old_backups() {
    local candidate basename
    while IFS= read -r -d '' candidate; do
        basename="$(basename -- "$candidate")"
        [[ "$basename" =~ ^20[0-9]{6}T[0-9]{6}Z$ ]] || continue
        [[ "$candidate" != "$BACKUP_DIR" ]] || continue
        printf 'Pruning local backup older than %s days: %s\n' \
            "$RETENTION_DAYS" "$candidate"
        if ! rm -rf -- "$candidate"; then
            printf 'ERROR: could not prune old backup %s\n' "$candidate" >&2
            return 1
        fi
    done < <(
        find "$BACKUP_ROOT" \
            -mindepth 1 \
            -maxdepth 1 \
            -type d \
            -name '20??????T??????Z' \
            -mtime "+$RETENTION_DAYS" \
            -print0
    )
}

finish() {
    local status=$?
    trap - EXIT
    if [[ "$APP_WAS_RUNNING" == true && "$APP_STOPPED" == true ]]; then
        printf 'Restarting the application...\n'
        if ! compose up --detach --wait --wait-timeout 300 app; then
            printf 'ERROR: backup finished but the application did not become ready\n' >&2
            status=1
        fi
    fi
    if [[ "$DB_WAS_RUNNING" == false ]]; then
        printf 'Returning the database to its previous stopped state...\n'
        if ! compose stop --timeout 60 db; then
            printf 'ERROR: backup finished but the database could not be stopped\n' >&2
            status=1
        fi
    fi
    if ((status == 0)) && ! prune_old_backups; then
        status=1
    fi
    exit "$status"
}
trap finish EXIT

compose config --quiet
DB_ID="$(compose ps --all --quiet db 2>/dev/null || true)"
if [[ -n "$DB_ID" ]]; then
    DB_STATE="$(docker_cmd inspect --format '{{.State.Status}}' "$DB_ID")"
    if [[ "$DB_STATE" != "running" && "$DB_STATE" != "restarting" \
        && "$DB_STATE" != "paused" ]]; then
        DB_WAS_RUNNING=false
    fi
    if [[ "$DB_STATE" == "paused" ]]; then
        compose unpause db
    fi
else
    DB_WAS_RUNNING=false
fi
compose up --detach --wait --wait-timeout 120 db

APP_ID="$(compose ps --all --quiet app 2>/dev/null || true)"
APP_STATE=""
if [[ -n "$APP_ID" ]]; then
    APP_STATE="$(docker_cmd inspect --format '{{.State.Status}}' "$APP_ID")"
fi
if [[ -n "$APP_ID" ]]; then
    if [[ "$APP_STATE" == "running" || "$APP_STATE" == "restarting" \
        || "$APP_STATE" == "paused" ]]; then
        APP_WAS_RUNNING=true
        APP_STOPPED=true
        printf 'Stopping the application briefly for a consistent browser-profile backup...\n'
    fi
    if [[ "$APP_STATE" == "paused" ]]; then
        compose unpause app
    fi
    # Stop unconditionally so an exited container under a restart policy
    # cannot become active between state inspection and the archive.
    compose stop --timeout 60 app
    APP_STATE="$(docker_cmd inspect --format '{{.State.Status}}' "$APP_ID")"
    [[ "$APP_STATE" != "running" && "$APP_STATE" != "restarting" \
        && "$APP_STATE" != "paused" ]] \
        || die "application could not be quiesced for backup"
fi

printf 'Dumping PostgreSQL...\n'
compose exec -T db pg_dump \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --format custom \
    --no-owner \
    --no-privileges \
    >"$BACKUP_DIR/database.dump"

printf 'Archiving application and browser-profile data...\n'
compose run --rm --no-deps --entrypoint tar app \
    --exclude=./.server.lock \
    -C /data \
    -czf - . \
    >"$BACKUP_DIR/app-data.tar.gz"

cp -- .env "$BACKUP_DIR/environment.env"
cp -- compose.yaml "$BACKUP_DIR/compose.yaml"
chmod 600 "$BACKUP_DIR/environment.env"

REVISION=unknown
if command -v git >/dev/null 2>&1 && git rev-parse --verify HEAD >/dev/null 2>&1; then
    REVISION="$(git rev-parse HEAD)"
fi

{
    printf 'created_utc=%s\n' "$TIMESTAMP"
    printf 'host=%s\n' "$(hostname)"
    printf 'database=%s\n' "$POSTGRES_DB"
    printf 'compose_project=twitch-drop-automator\n'
    printf 'revision=%s\n' "$REVISION"
} >"$BACKUP_DIR/manifest.txt"

(
    cd "$BACKUP_DIR"
    sha256sum \
        database.dump \
        app-data.tar.gz \
        environment.env \
        compose.yaml \
        manifest.txt \
        >SHA256SUMS
)

BACKUP_OWNER_UID="$(stat -c '%u' "$BACKUP_ROOT")"
BACKUP_OWNER_GID="$(stat -c '%g' "$BACKUP_ROOT")"
if ((EUID == 0)) && [[ "$BACKUP_OWNER_UID" != "0" ]]; then
    chown -R "$BACKUP_OWNER_UID:$BACKUP_OWNER_GID" "$BACKUP_DIR"
fi

printf 'Backup verified and written to %s (local retention: %s days)\n' \
    "$BACKUP_DIR" "$RETENTION_DAYS"
