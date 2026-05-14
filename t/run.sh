#!/bin/bash
# run.sh — run baseline regression test against one or all docker variants.
#
# Usage:
#   bash t/run.sh                       # run baseline on all 5 variants
#   bash t/run.sh ubuntu-24.04          # run baseline on one variant
#
# Baseline test: start nginx, curl http://localhost:<port>/ → expect 200 and
# body "ok", stop nginx cleanly. Task 13 will extend this driver to invoke
# t/regression/*.sh per variant with per-script applicability gating.
#
# Exit code: 0 iff every selected variant passes; nonzero on any failure.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# shellcheck disable=SC1091
source "$REPO_ROOT/t/regression/_common.sh"

ALL_VARIANTS=(
    ubuntu-22.04
    ubuntu-24.04
    ubuntu-24.04-shared-only
    ubuntu-26.04
    ubuntu-24.04-brotli
)

if [ "$#" -gt 0 ]; then
    VARIANTS=("$@")
else
    VARIANTS=("${ALL_VARIANTS[@]}")
fi

# Pick a non-conflicting host port per run so concurrent local invocations don't
# collide. Falls back to a default if the env override isn't set.
ZSTD_TEST_PORT="${ZSTD_TEST_PORT:-8080}"
export ZSTD_TEST_PORT

declare -a OK_LIST FAIL_LIST
OK_LIST=()
FAIL_LIST=()

baseline_test() {
    local variant="$1"
    local image="zstd-nginx-test:${variant}"

    if ! docker image inspect "$image" >/dev/null 2>&1; then
        echo "run.sh: image ${image} not found — run t/build.sh ${variant} first" >&2
        return 1
    fi

    # Ensure cleanup on any exit from this function.
    trap 'stop_nginx' RETURN

    if ! start_nginx "$image" "baseline-${variant}"; then
        echo "run.sh: start_nginx failed for ${variant}" >&2
        return 1
    fi

    local body status
    body="$(curl -fsS --max-time 5 "http://127.0.0.1:${ZSTD_TEST_PORT}/" 2>/dev/null)" || {
        echo "run.sh: GET / failed for ${variant}" >&2
        docker logs "$ZSTD_TEST_CID" >&2 || true
        return 1
    }

    if [ "$body" != "ok" ] && [ "$body" != "ok"$'\n' ] && [ "$(printf '%s' "$body" | tr -d '\n')" != "ok" ]; then
        echo "run.sh: GET / body mismatch for ${variant}: [${body}]" >&2
        return 1
    fi

    status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${ZSTD_TEST_PORT}/")"
    if [ "$status" != "200" ]; then
        echo "run.sh: GET / status ${status} != 200 for ${variant}" >&2
        return 1
    fi
}

for v in "${VARIANTS[@]}"; do
    echo "==> running baseline on ${v}"
    if baseline_test "$v"; then
        OK_LIST+=("$v")
    else
        FAIL_LIST+=("$v")
    fi
done

echo
echo "=== run summary ==="
for v in ${OK_LIST[@]+"${OK_LIST[@]}"}; do echo "  pass  ${v}"; done
for v in ${FAIL_LIST[@]+"${FAIL_LIST[@]}"}; do echo "  fail  ${v}"; done

if [ "${#FAIL_LIST[@]}" -gt 0 ]; then
    exit 1
fi
