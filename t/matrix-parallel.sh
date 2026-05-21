#!/bin/bash
# matrix-parallel.sh — build (buildx bake) + run (parallel pytest) the regression matrix.
#
# Sequential `bash t/build.sh && bash t/run.sh` over 6 variants takes ~45 min
# on Docker Desktop (linux/amd64 under Rosetta). This wrapper:
#   1. Uses `docker buildx bake` to build all 6 images in parallel with
#      BuildKit layer-cache sharing across targets (~3 min on warm cache).
#   2. Forks one subshell per variant that runs pytest via `t/run.sh` with
#      its own host port — so the 6 pytest runs land in ~7-10 min total
#      instead of ~42.
#
# Each pytest subshell writes its full stdout/stderr to
# `tmp/run/<variant>/_parallel.log`. The bake phase writes to
# `tmp/run/_bake.log`.
#
# Usage:
#   bash t/matrix-parallel.sh                       # all 6 default variants
#   bash t/matrix-parallel.sh ubuntu-22.04 ubuntu-24.04  # subset
#   ZSTD_TEST_PLATFORM=linux/amd64 bash t/matrix-parallel.sh  # force amd64
#   ZSTD_TEST_SKIP_BUILD=1 bash t/matrix-parallel.sh  # only run pytest
#
# Exit code: 0 iff bake AND every variant's pytest both pass.
#
# Why this shape (vs docker-compose): compose is service-mesh oriented and
# expects each "service" to be one long-running command + cross-service
# networking. Our harness uses `docker exec` into a long-lived sleep-infinity
# container per variant for pytest — compose adds YAML overhead without
# matching the model. buildx bake handles the build half natively (which IS
# compose-friendly territory) and the bash &-wrapper handles the docker-exec
# half (which isn't).

set -uo pipefail
# Intentionally not `set -e` — must reach the final summary even if a
# subshell exits non-zero, so the operator sees which variant broke.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

ALL_VARIANTS=(
    ubuntu-22.04
    ubuntu-24.04
    ubuntu-24.04-shared-only
    ubuntu-26.04
    ubuntu-24.04-brotli
    ubuntu-24.04-dynamic-brotli
)

# Map variant slug → bake target name. HCL identifiers can't contain dots,
# so the bake file uses dashes throughout (ubuntu-24-04 etc.). A simple
# `tr` covers every variant in ALL_VARIANTS — no per-variant case branch
# needed.
bake_target() {
    echo "$1" | tr '.' '-'
}

if [ "$#" -gt 0 ]; then
    VARIANTS=("$@")
    # Validate user-supplied variant names up front so a typo fails fast
    # with a clear message, instead of falling through to a noisy bake or
    # `t/run.sh` error a few minutes later. Angie variants are on-demand
    # only (see CLAUDE.md) — invoke `bash t/build.sh angie-<NN.NN>`
    # directly if you need them.
    for v in "${VARIANTS[@]}"; do
        ok=0
        for known in "${ALL_VARIANTS[@]}"; do
            if [ "$v" = "$known" ]; then
                ok=1
                break
            fi
        done
        if [ "$ok" -eq 0 ]; then
            echo "matrix-parallel: unknown variant '$v'." >&2
            echo "  known variants: ${ALL_VARIANTS[*]}" >&2
            exit 2
        fi
    done
else
    VARIANTS=("${ALL_VARIANTS[@]}")
fi

BASE_PORT="${ZSTD_TEST_BASE_PORT:-18080}"
LOG_DIR="${ZSTD_TEST_LOG_DIR:-tmp/run}"
mkdir -p "$LOG_DIR"

# ----------------------------------------------------------------------------
# Phase 1: build with docker buildx bake.
# ----------------------------------------------------------------------------

if [ -z "${ZSTD_TEST_SKIP_BUILD:-}" ]; then
    # All ALL_VARIANTS entries have a corresponding bake target — the
    # input-validation above guarantees we only see variants from that
    # set, so a single `docker buildx bake` call covers every variant.
    declare -a BAKE_TARGETS
    BAKE_TARGETS=()
    for v in "${VARIANTS[@]}"; do
        BAKE_TARGETS+=("$(bake_target "$v")")
    done

    bake_log="${LOG_DIR}/_bake.log"
    echo "=== matrix-parallel: bake ${#BAKE_TARGETS[@]} targets in parallel ==="
    echo "  log: ${bake_log}"
    # --load: write the built images into the local docker image store so
    # `docker run zstd-nginx-test:<variant>` (in t/run.sh) can find them.
    # Without --load the default `docker-container` buildx driver leaves
    # results in the BuildKit cache only, and t/run.sh's `docker images -q`
    # presence check fails on a clean machine. Equivalent long form is
    # `--set "*.output=type=docker"`.
    if ! docker buildx bake \
            -f t/docker/docker-bake.hcl \
            --load \
            ${ZSTD_TEST_PLATFORM:+--set "*.platform=$ZSTD_TEST_PLATFORM"} \
            "${BAKE_TARGETS[@]}" \
            > "$bake_log" 2>&1; then
        echo "matrix-parallel: bake FAILED — see ${bake_log}" >&2
        tail -n 40 "$bake_log" | sed 's/^/    /' >&2
        exit 2
    fi
    echo "matrix-parallel: build phase done"
else
    echo "=== matrix-parallel: ZSTD_TEST_SKIP_BUILD=1, skipping build ==="
fi

# ----------------------------------------------------------------------------
# Phase 2: parallel pytest runs.
# ----------------------------------------------------------------------------

declare -a PIDS VARIANT_NAMES VARIANT_PORTS VARIANT_LOGS
PIDS=()
VARIANT_NAMES=()
VARIANT_PORTS=()
VARIANT_LOGS=()

echo "=== matrix-parallel: launching ${#VARIANTS[@]} pytest runs ==="
for i in "${!VARIANTS[@]}"; do
    v="${VARIANTS[$i]}"
    port=$((BASE_PORT + i))
    mkdir -p "${LOG_DIR}/${v}"
    log="${LOG_DIR}/${v}/_parallel.log"
    VARIANT_NAMES+=("$v")
    VARIANT_PORTS+=("$port")
    VARIANT_LOGS+=("$log")
    {
        echo "[$(date -u +%H:%M:%S)] $v: run start (port=$port)" >&2
        ZSTD_TEST_PORT="$port" bash t/run.sh "$v" > "$log" 2>&1
        rc=$?
        echo "[$(date -u +%H:%M:%S)] $v: run done (rc=$rc)" >&2
        exit "$rc"
    } &
    last_pid=$!
    PIDS+=("$last_pid")
    # macOS ships bash 3.2 which has no negative array indexing, so we
    # capture $! via an explicit variable rather than ${PIDS[-1]}. The
    # latter aborts under `set -u` with "bad array subscript" on bash <4.3.
    echo "  -> $v (pid=$last_pid, port=$port, log=$log)"
done

echo "=== matrix-parallel: waiting for ${#PIDS[@]} jobs ==="

declare -a OK_LIST FAIL_LIST
OK_LIST=()
FAIL_LIST=()

for i in "${!PIDS[@]}"; do
    pid="${PIDS[$i]}"
    v="${VARIANT_NAMES[$i]}"
    log="${VARIANT_LOGS[$i]}"
    if wait "$pid"; then
        OK_LIST+=("$v")
    else
        rc=$?
        FAIL_LIST+=("${v} (rc=${rc})")
        echo "  --- tail ${log} (last 30 lines) ---"
        tail -n 30 "$log" 2>/dev/null | sed 's/^/    /'
        echo "  --- end tail ---"
    fi
done

echo
echo "=== matrix-parallel summary ==="
for v in ${OK_LIST[@]+"${OK_LIST[@]}"}; do echo "  ok    ${v}"; done
for v in ${FAIL_LIST[@]+"${FAIL_LIST[@]}"}; do echo "  fail  ${v}"; done

if [ "${#FAIL_LIST[@]}" -gt 0 ]; then
    exit 1
fi
