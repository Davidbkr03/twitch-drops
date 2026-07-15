#!/usr/bin/env bash
set -Eeuo pipefail

# shellcheck source=lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

STRICT=false
if (($# > 1)); then
    printf 'Usage: sudo bash scripts/doctor.sh [--strict]\n' >&2
    exit 2
fi
case "${1:-}" in
    "") ;;
    --strict) STRICT=true ;;
    *)
        printf 'Usage: sudo bash scripts/doctor.sh [--strict]\n' >&2
        exit 2
        ;;
esac

FAILURES=0
WARNINGS=0

pass() {
    printf 'PASS  %s\n' "$*"
}

fail() {
    printf 'FAIL  %s\n' "$*" >&2
    FAILURES=$((FAILURES + 1))
}

warn() {
    printf 'WARN  %s\n' "$*" >&2
    WARNINGS=$((WARNINGS + 1))
}

operational_alert() {
    if [[ "$STRICT" == true ]]; then
        fail "$@"
    else
        warn "$@"
    fi
}

if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    if [[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" == 24.04 ]]; then
        pass "host is Ubuntu 24.04"
    else
        fail "supported host is Ubuntu 24.04 (found ${PRETTY_NAME:-unknown})"
    fi
else
    fail "cannot identify the host operating system"
fi

docker_cmd info >/dev/null 2>&1 && pass "Docker daemon is reachable" \
    || fail "Docker daemon is not reachable"
compose version >/dev/null 2>&1 && pass "Docker Compose plugin is available" \
    || fail "Docker Compose plugin is unavailable"

ENV_MODE="$(stat -c '%a' .env 2>/dev/null || true)"
[[ "$ENV_MODE" == "600" ]] && pass ".env permissions are 600" \
    || fail ".env permissions are $ENV_MODE, expected 600"

BIND_ADDRESS="$(env_value BIND_ADDRESS)"
SECRET_KEY="$(env_value SECRET_KEY)"
BOOTSTRAP_TOKEN="$(env_value BOOTSTRAP_TOKEN)"
POSTGRES_PASSWORD="$(env_value POSTGRES_PASSWORD)"
POSTGRES_USER_VALUE="$(env_value POSTGRES_USER)"
POSTGRES_DB_VALUE="$(env_value POSTGRES_DB)"
BACKUP_ROOT_VALUE="$(env_value BACKUP_ROOT)"
if is_private_ipv4 "$BIND_ADDRESS"; then
    pass "a private LAN/VPN bind address is configured"
else
    fail "BIND_ADDRESS is missing, public, or exposes all interfaces"
fi
if [[ ${#SECRET_KEY} -ge 48 && "$SECRET_KEY" != replace-* && "$SECRET_KEY" =~ ^[A-Za-z0-9._~-]+$ ]]; then
    pass "SECRET_KEY is present and non-placeholder"
else
    fail "SECRET_KEY is missing, short, or a placeholder"
fi
if [[ ${#POSTGRES_PASSWORD} -ge 32 && "$POSTGRES_PASSWORD" != replace-* && "$POSTGRES_PASSWORD" =~ ^[A-Za-z0-9._~-]+$ ]]; then
    pass "POSTGRES_PASSWORD is present and non-placeholder"
else
    fail "POSTGRES_PASSWORD is missing, short, or a placeholder"
fi
if [[ ${#BOOTSTRAP_TOKEN} -ge 32 && "$BOOTSTRAP_TOKEN" != replace-* && "$BOOTSTRAP_TOKEN" =~ ^[A-Za-z0-9._~-]+$ ]]; then
    pass "BOOTSTRAP_TOKEN is present and non-placeholder"
else
    fail "BOOTSTRAP_TOKEN is missing, short, or a placeholder"
fi
if [[ "${POSTGRES_USER_VALUE:-twitch}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ \
    && "${POSTGRES_DB_VALUE:-twitch_drops}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    pass "PostgreSQL identifiers are safe for the application DSN"
else
    fail "POSTGRES_USER or POSTGRES_DB is not a simple PostgreSQL identifier"
fi

if compose config --quiet >/dev/null 2>&1; then
    pass "Compose configuration is valid"
else
    fail "Compose configuration is invalid"
fi

APP_ID="$(compose ps -q app 2>/dev/null || true)"
DB_ID="$(compose ps -q db 2>/dev/null || true)"
if [[ -n "$APP_ID" ]]; then
    APP_HEALTH="$(docker_cmd inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$APP_ID" 2>/dev/null || true)"
    [[ "$APP_HEALTH" == healthy ]] && pass "application container is healthy" \
        || fail "application container health is ${APP_HEALTH:-unknown}"
else
    fail "application container does not exist"
fi
if [[ -n "$DB_ID" ]]; then
    DB_HEALTH="$(docker_cmd inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$DB_ID" 2>/dev/null || true)"
    [[ "$DB_HEALTH" == healthy ]] && pass "database container is healthy" \
        || fail "database container health is ${DB_HEALTH:-unknown}"
else
    fail "database container does not exist"
fi

if compose exec -T app python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/health/live', timeout=5).read()" \
    >/dev/null 2>&1; then
    pass "liveness endpoint responds"
else
    fail "liveness endpoint does not respond"
fi
if compose exec -T app python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/health/ready', timeout=5).read()" \
    >/dev/null 2>&1; then
    pass "readiness endpoint confirms dependencies"
else
    fail "readiness endpoint reports unavailable"
fi
if compose exec -T db pg_isready --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    >/dev/null 2>&1; then
    pass "PostgreSQL accepts connections"
else
    fail "PostgreSQL is not accepting connections"
fi

check_filesystem_usage() {
    local label="$1"
    local path="$2"
    local usage
    usage="$(df -P "$path" 2>/dev/null | awk 'NR==2 {gsub(/%/, "", $5); print $5}')"
    if [[ "$usage" =~ ^[0-9]+$ && "$usage" -lt 85 ]]; then
        pass "$label filesystem usage is ${usage}%"
    else
        operational_alert "$label filesystem usage is ${usage:-unknown}% (investigate at 85% or higher)"
    fi
}

check_filesystem_usage "checkout" "$ROOT_DIR"
DOCKER_ROOT="$(docker_cmd info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
if [[ -n "$DOCKER_ROOT" && -d "$DOCKER_ROOT" ]]; then
    check_filesystem_usage "Docker data" "$DOCKER_ROOT"
else
    operational_alert "Docker data-root filesystem could not be inspected"
fi

if [[ -n "$BACKUP_ROOT_VALUE" && -d "$BACKUP_ROOT_VALUE" ]]; then
    check_filesystem_usage "backup" "$BACKUP_ROOT_VALUE"
    LATEST_BACKUP="$(find "$BACKUP_ROOT_VALUE" \
        -mindepth 1 -maxdepth 1 -type d -name '20??????T??????Z' \
        -printf '%f\n' 2>/dev/null | sort | tail -n 1)"
    if [[ -n "$LATEST_BACKUP" \
        && -f "$BACKUP_ROOT_VALUE/$LATEST_BACKUP/manifest.txt" \
        && -f "$BACKUP_ROOT_VALUE/$LATEST_BACKUP/SHA256SUMS" ]]; then
        LATEST_BACKUP_EPOCH="$(stat -c '%Y' "$BACKUP_ROOT_VALUE/$LATEST_BACKUP")"
        BACKUP_AGE_SECONDS=$(($(date +%s) - LATEST_BACKUP_EPOCH))
        if ((BACKUP_AGE_SECONDS <= 172800)); then
            pass "latest completed backup is less than 48 hours old"
        else
            operational_alert "latest completed backup is more than 48 hours old"
        fi
    else
        operational_alert "no completed timestamped backup was found in BACKUP_ROOT"
    fi
else
    operational_alert "BACKUP_ROOT is not configured or its directory is unavailable"
fi

AVAILABLE_KIB="$(awk '/MemAvailable:/ {print $2}' /proc/meminfo 2>/dev/null || true)"
if [[ "$AVAILABLE_KIB" =~ ^[0-9]+$ && "$AVAILABLE_KIB" -ge 2097152 ]]; then
    pass "at least 2 GiB memory is currently available"
else
    warn "less than 2 GiB memory is currently available"
fi

if command -v timedatectl >/dev/null 2>&1; then
    [[ "$(timedatectl show --property=NTPSynchronized --value 2>/dev/null || true)" == yes ]] \
        && pass "host clock is synchronized" \
        || warn "host clock is not reporting NTP synchronization"
fi

printf '\nDiagnostics complete: %d failure(s), %d warning(s).\n' "$FAILURES" "$WARNINGS"
if ((FAILURES > 0)); then
    printf 'Inspect recent logs with: docker compose logs --tail=200 app db\n' >&2
    exit 1
fi
