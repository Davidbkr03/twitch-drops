#!/usr/bin/env bash
set -Eeuo pipefail

# shellcheck source=lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
acquire_operation_lock

((EUID != 0)) \
    || die "run update.sh as the checkout owner without sudo; it escalates Docker only when needed"
command -v git >/dev/null 2>&1 || die "git is required to update the checkout"
[[ "$(git branch --show-current)" == main ]] \
    || die "the deployed checkout must be on main"
[[ -z "$(git status --porcelain)" ]] \
    || die "the checkout has local changes; update cancelled"

OLD_REVISION="$(git rev-parse HEAD)"
git fetch --prune origin main
NEW_REVISION="$(git rev-parse origin/main)"

if [[ "$OLD_REVISION" != "$NEW_REVISION" ]]; then
    git merge-base --is-ancestor "$OLD_REVISION" "$NEW_REVISION" \
        || die "origin/main is not a fast-forward from this checkout"
else
    printf 'Source is already at %s; reconciling the deployment anyway.\n' "$OLD_REVISION"
fi

printf 'Creating a pre-update backup...\n'
bash "$ROOT_DIR/scripts/backup.sh" "$ROOT_DIR/backups"

on_error() {
    local status=$?
    trap - ERR
    printf 'ERROR: update failed. The pre-update backup is under %s/backups.\n' "$ROOT_DIR" >&2
    printf 'Previous revision: %s\n' "$OLD_REVISION" >&2
    printf 'Review container logs before attempting a manual rollback.\n' >&2
    exit "$status"
}
trap on_error ERR

if [[ "$OLD_REVISION" != "$NEW_REVISION" ]]; then
    git merge --ff-only "$NEW_REVISION"
fi
compose config --quiet
compose pull db
compose build --pull app
wait_for_stack 300
bash "$ROOT_DIR/scripts/doctor.sh"

trap - ERR
printf 'Deployment reconciled successfully from %s to %s.\n' "$OLD_REVISION" "$NEW_REVISION"
