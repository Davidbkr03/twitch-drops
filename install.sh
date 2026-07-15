#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

if [[ ! -f compose.yaml || ! -f Dockerfile ]]; then
    die "run this script from a complete repository checkout"
fi

if [[ ! -r /etc/os-release ]]; then
    die "cannot identify the operating system"
fi

# shellcheck disable=SC1091
. /etc/os-release
if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "24.04" ]]; then
    die "the supported deployment host is Ubuntu Server 24.04"
fi

if ((EUID == 0)); then
    SUDO=()
    [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]] \
        || die "run install.sh as the unprivileged checkout owner; it invokes sudo when needed"
    LOGIN_USER="$SUDO_USER"
else
    command -v sudo >/dev/null 2>&1 || die "sudo is required"
    SUDO=(sudo)
    LOGIN_USER="$(id -un)"
fi

CHECKOUT_OWNER="$(stat -c '%U' "$SCRIPT_DIR")"
[[ "$CHECKOUT_OWNER" != "root" && "$CHECKOUT_OWNER" == "$LOGIN_USER" ]] \
    || die "the checkout must be owned by and installed as the unprivileged operator account"

install_docker() {
    printf 'Installing Docker Engine and the Compose plugin...\n'
    # Docker documents these as conflicting packages. A clean appliance VM
    # should not mix distro Docker/containerd packages with Docker CE.
    "${SUDO[@]}" apt-get remove -y \
        docker.io docker-compose docker-compose-v2 docker-doc podman-docker \
        containerd runc || true
    "${SUDO[@]}" apt-get update
    "${SUDO[@]}" apt-get install -y ca-certificates curl gnupg openssl
    "${SUDO[@]}" install -m 0755 -d /etc/apt/keyrings
    "${SUDO[@]}" curl -fsSL \
        https://download.docker.com/linux/ubuntu/gpg \
        -o /etc/apt/keyrings/docker.asc
    "${SUDO[@]}" chmod a+r /etc/apt/keyrings/docker.asc

    local architecture
    architecture="$(dpkg --print-architecture)"
    printf '%s\n' \
        "deb [arch=${architecture} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
        | "${SUDO[@]}" tee /etc/apt/sources.list.d/docker.list >/dev/null

    "${SUDO[@]}" apt-get update
    "${SUDO[@]}" apt-get install -y \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    "${SUDO[@]}" systemctl enable --now docker

    if [[ "$LOGIN_USER" != "root" ]]; then
        "${SUDO[@]}" usermod -aG docker "$LOGIN_USER"
    fi
}

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    install_docker
else
    printf 'Docker Engine and Docker Compose are already installed.\n'
    "${SUDO[@]}" systemctl enable --now docker
fi

command -v openssl >/dev/null 2>&1 \
    || "${SUDO[@]}" apt-get install -y openssl

if docker info >/dev/null 2>&1; then
    DOCKER=(docker)
else
    DOCKER=("${SUDO[@]}" docker)
    "${DOCKER[@]}" info >/dev/null \
        || die "Docker daemon is unavailable"
fi

volume_exists() {
    "${DOCKER[@]}" volume inspect "$1" >/dev/null 2>&1
}

check_existing_volumes() {
    local intended_browser="twitch-drop-automator_browser_data"
    local intended_database="twitch-drop-automator_postgres_data"
    local browser_exists=0
    local database_exists=0
    local volume_list volume metadata project logical_name
    local -A legacy_projects=()
    local -A legacy_browser=()
    local -A legacy_database=()

    volume_exists "$intended_browser" && browser_exists=1
    volume_exists "$intended_database" && database_exists=1

    if ((browser_exists != database_exists)); then
        die "only one production data volume exists; recover the missing volume before deployment"
    fi

    # Older Compose releases derived volume names from the checkout directory.
    # Scan all volumes, even when the canonical pair exists, so an empty failed
    # install cannot hide the real data under another project name.
    if ! volume_list="$("${DOCKER[@]}" volume ls --quiet)"; then
        die "Docker volumes could not be enumerated safely"
    fi
    while IFS= read -r volume; do
        [[ -n "$volume" ]] || continue
        if ! metadata="$("${DOCKER[@]}" volume inspect --format \
            '{{ index .Labels "com.docker.compose.project" }}|{{ index .Labels "com.docker.compose.volume" }}' \
            "$volume")"; then
            die "Docker volume metadata could not be inspected for $volume"
        fi
        IFS='|' read -r project logical_name <<<"$metadata"
        if [[ -z "$project" || "$project" == "<no value>" \
            || -z "$logical_name" || "$logical_name" == "<no value>" ]]; then
            if [[ "$volume" =~ ^(.+)_(browser_data|postgres_data)$ ]]; then
                project="${BASH_REMATCH[1]}"
                logical_name="${BASH_REMATCH[2]}"
            else
                continue
            fi
        fi
        [[ "$project" != "twitch-drop-automator" ]] || continue
        case "${project,,}" in
            *twitch*drop*|*twitch*automat*) ;;
            *) continue ;;
        esac
        case "$logical_name" in
            browser_data) legacy_browser["$project"]="$volume" ;;
            postgres_data) legacy_database["$project"]="$volume" ;;
            *) continue ;;
        esac
        legacy_projects["$project"]=1
    done <<<"$volume_list"

    for project in "${!legacy_projects[@]}"; do
        if ((browser_exists == 1)); then
            die "canonical and legacy Twitch volumes coexist (legacy project '${project}'); follow docs/operations.md before installing"
        fi
        if [[ -z "${legacy_browser[$project]:-}" \
            || -z "${legacy_database[$project]:-}" ]]; then
            die "an incomplete legacy Twitch volume set exists for project '${project}'; recover it before installing"
        fi
        die "legacy Compose volumes for project '${project}' were detected; follow the legacy-volume recovery procedure in docs/operations.md before installing"
    done

    if ((browser_exists == 1)); then
        [[ -f .env ]] \
            || die "production data volumes already exist but .env is missing; restore the original .env before deployment"
        [[ "${RESUME_INSTALL:-0}" == "1" ]] \
            || die "an existing production data set was detected; use scripts/update.sh for an installed service, or set RESUME_INSTALL=1 only to resume a verified incomplete first install"
    fi
}

check_existing_volumes

is_ipv4() {
    local address="$1"
    local octet
    local -a octets
    IFS=. read -r -a octets <<<"$address"
    [[ ${#octets[@]} -eq 4 ]] || return 1
    for octet in "${octets[@]}"; do
        [[ "$octet" =~ ^[0-9]{1,3}$ ]] || return 1
        ((10#$octet <= 255)) || return 1
    done
}

is_private_ipv4() {
    local address="$1"
    local first second _rest
    IFS=. read -r first second _rest <<<"$address"
    [[ "$first" == "10" || "$first" == "127" ]] && return 0
    [[ "$first" == "192" && "$second" == "168" ]] && return 0
    [[ "$first" == "172" && 10#$second -ge 16 && 10#$second -le 31 ]] && return 0
    [[ "$first" == "100" && 10#$second -ge 64 && 10#$second -le 127 ]] && return 0
    return 1
}

detect_private_ipv4() {
    local candidate
    candidate="$(ip -4 route get 1.1.1.1 2>/dev/null \
        | awk '{for (i = 1; i <= NF; i++) if ($i == "src") {print $(i + 1); exit}}' \
        || true)"
    if is_ipv4 "$candidate" && is_private_ipv4 "$candidate" && [[ "$candidate" != 127.* ]]; then
        printf '%s\n' "$candidate"
        return 0
    fi
    while read -r candidate; do
        if is_ipv4 "$candidate" && is_private_ipv4 "$candidate" && [[ "$candidate" != 127.* ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done < <(hostname -I 2>/dev/null | tr ' ' '\n')
    return 1
}

validate_port() {
    local port="$1"
    [[ "$port" =~ ^[0-9]+$ ]] || die "PORT must be a number"
    ((10#$port >= 1 && 10#$port <= 65535)) || die "PORT must be between 1 and 65535"
}

env_value() {
    local key="$1"
    sed -n "s/^${key}=//p" .env | tail -n 1
}

[[ ! -L .env ]] || die ".env must be a regular file, not a symlink"
if [[ ! -e .env ]]; then
    BIND_ADDRESS="${BIND_ADDRESS:-$(detect_private_ipv4 || true)}"
    PORT="${PORT:-5000}"
    [[ -n "$BIND_ADDRESS" ]] \
        || die "set BIND_ADDRESS to the VM's static private IPv4 address"
    is_ipv4 "$BIND_ADDRESS" || die "BIND_ADDRESS must be an IPv4 address"
    is_private_ipv4 "$BIND_ADDRESS" \
        || die "BIND_ADDRESS must be a private LAN, loopback, or carrier-grade NAT/VPN address"
    validate_port "$PORT"

    if [[ "$BIND_ADDRESS" != 127.* ]] \
        && ! ip -4 -o address show | awk '{print $4}' | cut -d/ -f1 | grep -Fxq "$BIND_ADDRESS"; then
        die "BIND_ADDRESS is not currently assigned to this VM"
    fi

    SECRET_KEY="$(openssl rand -hex 48)"
    BOOTSTRAP_TOKEN="$(openssl rand -hex 32)"
    POSTGRES_PASSWORD="$(openssl rand -hex 48)"
    {
        printf 'BIND_ADDRESS=%s\n' "$BIND_ADDRESS"
        printf 'PORT=%s\n' "$PORT"
        printf 'SECRET_KEY=%s\n' "$SECRET_KEY"
        printf 'BOOTSTRAP_TOKEN=%s\n' "$BOOTSTRAP_TOKEN"
        printf 'POSTGRES_USER=twitch\n'
        printf 'POSTGRES_PASSWORD=%s\n' "$POSTGRES_PASSWORD"
        printf 'POSTGRES_DB=twitch_drops\n'
        printf 'BACKUP_ROOT=/srv/backups/twitch-drop-automator\n'
    } >.env
    printf 'Created .env with generated secrets.\n'
else
    [[ -f .env && ! -L .env ]] || die ".env must be a regular file, not a symlink"
    chmod 600 .env
    BIND_ADDRESS="$(env_value BIND_ADDRESS)"
    PORT="$(env_value PORT)"
    SECRET_KEY="$(env_value SECRET_KEY)"
    BOOTSTRAP_TOKEN="$(env_value BOOTSTRAP_TOKEN)"
    POSTGRES_PASSWORD="$(env_value POSTGRES_PASSWORD)"
    POSTGRES_USER="$(env_value POSTGRES_USER)"
    POSTGRES_DB="$(env_value POSTGRES_DB)"
    BACKUP_ROOT="$(env_value BACKUP_ROOT)"
    [[ -n "$BIND_ADDRESS" && -n "$SECRET_KEY" && -n "$BOOTSTRAP_TOKEN" \
        && -n "$POSTGRES_PASSWORD" ]] \
        || die ".env is missing BIND_ADDRESS, SECRET_KEY, BOOTSTRAP_TOKEN, or POSTGRES_PASSWORD"
    [[ "$SECRET_KEY" != replace-* && "$BOOTSTRAP_TOKEN" != replace-* \
        && "$POSTGRES_PASSWORD" != replace-* ]] \
        || die ".env still contains placeholder secrets"
    [[ ${#SECRET_KEY} -ge 48 && "$SECRET_KEY" =~ ^[A-Za-z0-9._~-]+$ ]] \
        || die "SECRET_KEY must be at least 48 URL-safe characters"
    [[ ${#POSTGRES_PASSWORD} -ge 32 && "$POSTGRES_PASSWORD" =~ ^[A-Za-z0-9._~-]+$ ]] \
        || die "POSTGRES_PASSWORD must be at least 32 URL-safe characters"
    [[ ${#BOOTSTRAP_TOKEN} -ge 32 && "$BOOTSTRAP_TOKEN" =~ ^[A-Za-z0-9._~-]+$ ]] \
        || die "BOOTSTRAP_TOKEN must be at least 32 URL-safe characters"
    [[ "${POSTGRES_USER:-twitch}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
        || die "POSTGRES_USER must be a simple PostgreSQL identifier"
    [[ "${POSTGRES_DB:-twitch_drops}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
        || die "POSTGRES_DB must be a simple PostgreSQL identifier"
    is_ipv4 "$BIND_ADDRESS" || die "BIND_ADDRESS in .env must be an IPv4 address"
    is_private_ipv4 "$BIND_ADDRESS" \
        || die "BIND_ADDRESS in .env must be private LAN/VPN address"
    validate_port "${PORT:-5000}"
    [[ -z "$BACKUP_ROOT" || "$BACKUP_ROOT" == "/srv/backups/twitch-drop-automator" ]] \
        || die "the supported BACKUP_ROOT is /srv/backups/twitch-drop-automator"
    printf 'Using the existing .env without replacing its secrets.\n'
fi
chmod 600 .env
if ((EUID == 0)); then
    chown "$LOGIN_USER" .env
fi

BACKUP_ROOT="$(env_value BACKUP_ROOT)"
if [[ -n "$BACKUP_ROOT" ]]; then
    [[ "$BACKUP_ROOT" == "/srv/backups/twitch-drop-automator" \
        && "$(realpath -m -- "$BACKUP_ROOT")" == "$BACKUP_ROOT" ]] \
        || die "BACKUP_ROOT must be the dedicated non-symlink path /srv/backups/twitch-drop-automator"
    if [[ -e /srv/backups || -L /srv/backups ]]; then
        [[ -d /srv/backups && ! -L /srv/backups ]] \
            || die "/srv/backups exists but is not a real directory"
    else
        "${SUDO[@]}" install -d -m 0755 /srv/backups
    fi
    sudo -u "$LOGIN_USER" test -x /srv/backups \
        || die "/srv/backups is not traversable by the operator account"
    if [[ -e "$BACKUP_ROOT" || -L "$BACKUP_ROOT" ]]; then
        [[ -d "$BACKUP_ROOT" && ! -L "$BACKUP_ROOT" ]] \
            || die "BACKUP_ROOT exists but is not a real directory"
        BACKUP_OWNER="$(stat -c '%U' "$BACKUP_ROOT")"
        BACKUP_MODE="$(stat -c '%a' "$BACKUP_ROOT")"
        [[ "$BACKUP_OWNER" == "$LOGIN_USER" && "$BACKUP_MODE" == "700" ]] \
            || die "BACKUP_ROOT must be owned by $LOGIN_USER with mode 700; fix it explicitly before installing"
    else
        LOGIN_GROUP="$(id -gn "$LOGIN_USER")"
        "${SUDO[@]}" install -d -o "$LOGIN_USER" -g "$LOGIN_GROUP" \
            -m 0700 "$BACKUP_ROOT"
    fi
fi

printf 'Validating deployment configuration...\n'
"${DOCKER[@]}" compose config --quiet

printf 'Pulling and building pinned production images...\n'
"${DOCKER[@]}" compose pull db
"${DOCKER[@]}" compose build --pull app

printf 'Starting the stack and waiting for readiness...\n'
"${DOCKER[@]}" compose up \
    --detach \
    --remove-orphans \
    --wait \
    --wait-timeout 300

"${DOCKER[@]}" compose ps

printf '\nDeployment is healthy: http://%s:%s\n' "$BIND_ADDRESS" "${PORT:-5000}"
printf 'Retrieve the first-account token with:\n'
printf "  sed -n 's/^BOOTSTRAP_TOKEN=//p' .env\n"
printf 'Create the first account immediately; registration closes after that account.\n'
printf 'Run diagnostics with: sudo bash scripts/doctor.sh\n'
if [[ "$LOGIN_USER" != "root" ]]; then
    printf 'Log out and back in once before using Docker without sudo.\n'
fi
