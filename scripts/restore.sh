#!/usr/bin/env bash
set -Eeuo pipefail

# shellcheck source=lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
acquire_operation_lock

usage() {
    printf 'Usage: sudo bash scripts/restore.sh BACKUP_DIRECTORY [--yes]\n' >&2
    exit 2
}

[[ $# -ge 1 && $# -le 2 ]] || usage
[[ -d "$1" ]] || die "backup directory does not exist: $1"
BACKUP_DIR="$(cd -- "$1" && pwd)"
ASSUME_YES="${2:-}"
[[ -z "$ASSUME_YES" || "$ASSUME_YES" == "--yes" ]] || usage

for required in database.dump app-data.tar.gz environment.env compose.yaml manifest.txt SHA256SUMS; do
    [[ -f "$BACKUP_DIR/$required" ]] || die "backup is missing $required"
done

manifest_value() {
    local key="$1"
    sed -n "s/^${key}=//p" "$BACKUP_DIR/manifest.txt" | tail -n 1
}

printf 'Verifying backup checksums...\n'
(
    cd "$BACKUP_DIR"
    sha256sum --check SHA256SUMS
)

tar -tzf "$BACKUP_DIR/app-data.tar.gz" >/dev/null \
    || die "application-data archive is unreadable"
if tar -tzf "$BACKUP_DIR/app-data.tar.gz" \
    | awk '/^\// || /(^|\/)\.\.($|\/)/ { unsafe=1 } END { exit unsafe ? 0 : 1 }'; then
    die "application-data archive contains an unsafe path"
fi

if ! cmp -s "$ROOT_DIR/.env" "$BACKUP_DIR/environment.env"; then
    printf 'WARNING: the active .env differs from the backup.\n' >&2
    printf 'The active database credentials will be retained; review the backed-up file separately.\n' >&2
fi

[[ "$(manifest_value compose_project)" == "twitch-drop-automator" ]] \
    || die "backup manifest belongs to a different Compose project"
[[ "$(manifest_value database)" == "$POSTGRES_DB" ]] \
    || die "backup database does not match the configured database"

if [[ "$ASSUME_YES" != "--yes" ]]; then
    printf '\nThis permanently replaces the current database and application data.\n'
    printf 'Type RESTORE to continue: '
    read -r confirmation
    [[ "$confirmation" == "RESTORE" ]] || die "restore cancelled"
fi

compose config --quiet
compose up --detach --wait --wait-timeout 120 db

printf 'Validating the PostgreSQL dump before changing active data...\n'
compose exec -T db pg_restore --list <"$BACKUP_DIR/database.dump" >/dev/null

SAFETY_ROOT="$(dirname -- "$BACKUP_DIR")/pre-restore-safety"
printf 'Creating a safety backup of the currently active state...\n'
bash "$ROOT_DIR/scripts/backup.sh" "$SAFETY_ROOT"

printf 'Stopping the application...\n'
APP_ID="$(compose ps --all --quiet app 2>/dev/null || true)"
if [[ -n "$APP_ID" ]]; then
    APP_STATE="$(docker_cmd inspect --format '{{.State.Status}}' "$APP_ID")"
    if [[ "$APP_STATE" == "paused" ]]; then
        compose unpause app
    fi
    compose stop --timeout 60 app
    APP_STATE="$(docker_cmd inspect --format '{{.State.Status}}' "$APP_ID")"
    [[ "$APP_STATE" != "running" && "$APP_STATE" != "restarting" \
        && "$APP_STATE" != "paused" ]] \
        || die "application is still active; restore cancelled before data replacement"
fi

REPLACEMENT_STARTED=false
on_error() {
    local status=$?
    trap - ERR
    if [[ "$REPLACEMENT_STARTED" == true ]]; then
        compose stop --timeout 60 app >/dev/null 2>&1 || true
        printf 'ERROR: restore failed after replacement began; the app has been stopped.\n' >&2
        printf 'Retry from the untouched backup after reviewing the error.\n' >&2
    fi
    exit "$status"
}
trap on_error ERR

printf 'Replacing PostgreSQL data...\n'
REPLACEMENT_STARTED=true
compose exec -T db dropdb \
    --username "$POSTGRES_USER" \
    --if-exists \
    --force \
    "$POSTGRES_DB"
compose exec -T db createdb \
    --username "$POSTGRES_USER" \
    --owner "$POSTGRES_USER" \
    "$POSTGRES_DB"
compose exec -T db pg_restore \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --no-owner \
    --no-privileges \
    --exit-on-error \
    <"$BACKUP_DIR/database.dump"

printf 'Replacing application and browser-profile data...\n'
compose run --rm --no-deps --entrypoint sh app -ceu \
    'find /data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; tar -xzf - -C /data' \
    <"$BACKUP_DIR/app-data.tar.gz"

printf 'Starting the restored stack and applying current migrations...\n'
wait_for_stack 300
compose ps
trap - ERR
printf 'Restore completed successfully.\n'
