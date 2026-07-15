#!/usr/bin/env bash

if [[ -n "${TWITCH_DROPS_LIB_LOADED:-}" ]]; then
    return 0
fi
readonly TWITCH_DROPS_LIB_LOADED=1

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ROOT_DIR
cd "$ROOT_DIR"

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

[[ -f compose.yaml && -f .env ]] \
    || die "compose.yaml and .env must exist in $ROOT_DIR"

if docker info >/dev/null 2>&1; then
    DOCKER=(docker)
elif command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
    DOCKER=(sudo -n docker)
else
    die "Docker is unavailable; log in again for docker-group access or run this script with sudo"
fi
readonly -a DOCKER

compose() {
    "${DOCKER[@]}" compose --project-directory "$ROOT_DIR" "$@"
}

docker_cmd() {
    "${DOCKER[@]}" "$@"
}

acquire_operation_lock() {
    if [[ "${TWITCH_DROPS_OPERATION_LOCK_HELD:-}" == "1" ]]; then
        return 0
    fi
    command -v flock >/dev/null 2>&1 || die "flock is required for safe operations"
    # Lock the checkout directory read-only so both the unprivileged updater
    # and root-run backup/restore jobs coordinate without creating files that
    # later have conflicting ownership.
    exec 9<"$ROOT_DIR"
    flock -n 9 \
        || die "another backup, restore, or update operation is already running"
    export TWITCH_DROPS_OPERATION_LOCK_HELD=1
}

env_value() {
    local key="$1"
    sed -n "s/^${key}=//p" "$ROOT_DIR/.env" | tail -n 1
}

is_private_ipv4() {
    local address="$1"
    local first second third fourth
    IFS=. read -r first second third fourth <<<"$address"
    for octet in "$first" "$second" "$third" "$fourth"; do
        [[ "$octet" =~ ^[0-9]{1,3}$ ]] || return 1
        ((10#$octet <= 255)) || return 1
    done
    [[ "$first" == "10" || "$first" == "127" ]] && return 0
    [[ "$first" == "192" && "$second" == "168" ]] && return 0
    [[ "$first" == "172" && 10#$second -ge 16 && 10#$second -le 31 ]] && return 0
    [[ "$first" == "100" && 10#$second -ge 64 && 10#$second -le 127 ]] && return 0
    return 1
}

POSTGRES_USER="$(env_value POSTGRES_USER)"
POSTGRES_DB="$(env_value POSTGRES_DB)"
readonly POSTGRES_USER="${POSTGRES_USER:-twitch}"
readonly POSTGRES_DB="${POSTGRES_DB:-twitch_drops}"

wait_for_stack() {
    compose up --detach --remove-orphans --wait --wait-timeout "${1:-300}"
}
