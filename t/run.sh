#!/bin/bash
# run.sh — run the V1 regression suite against one or all docker variants.
#
# Usage:
#   bash t/run.sh                       # all 5 variants
#   bash t/run.sh ubuntu-24.04          # one variant
#   bash t/run.sh ubuntu-22.04 ubuntu-24.04-brotli  # subset
#
# For each selected variant:
#   1. Start a fresh container detached, mapping the test port to 8080.
#   2. Run the baseline liveness probe (GET / → 200 "ok").
#   3. Stop nginx inside the container so individual regression scripts can
#      re-render the config and bring nginx up themselves.
#   4. Execute every t/regression/*.sh inside the container via `docker exec`,
#      skipping scripts that don't apply to the current variant.
#   5. Stop the container.
#
# Applicability gating:
#   * `filter-priority.sh` only runs on the brotli-enabled variant.
#   * Other scripts run on every variant.
#
# The driver keeps running after a script failure so all failures are visible
# in one pass. Exit code is 0 iff every selected variant passes every applicable
# script.
#
# Per-script log directory (default tmp/run/<variant>/<script>.log). Override
# with $ZSTD_RUN_LOG_DIR.
#
# Compatibility note: this script targets bash 3.2 (the macOS system bash). No
# `mapfile`, `declare -A`, or `find -printf` — those are bash 4+/GNU-find only.

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

ZSTD_TEST_PORT="${ZSTD_TEST_PORT:-8080}"
export ZSTD_TEST_PORT

LOG_DIR="${ZSTD_RUN_LOG_DIR:-${REPO_ROOT}/tmp/run}"
mkdir -p "$LOG_DIR"

# Discover regression scripts (bash 3.2 compatible — no `mapfile`, no
# `find -printf`).
REG_SCRIPTS=()
for f in "$REPO_ROOT"/t/regression/*.sh; do
    name="$(basename "$f")"
    [ "$name" = "_common.sh" ] && continue
    REG_SCRIPTS+=("$name")
done

script_applies() {
    local script="$1" variant="$2"
    case "$script" in
        filter-priority.sh)
            [ "$variant" = "ubuntu-24.04-brotli" ]
            ;;
        *)
            return 0
            ;;
    esac
}

# Per-variant aggregate counters kept as parallel arrays (bash 3.2 has no
# associative arrays). variant_index() returns the slot for a given variant.
VAR_NAMES=()
VAR_PASS=()
VAR_FAIL=()
# variant_index writes the (zero-based) row index for $1 into $VARIANT_IDX,
# appending a new row if needed. We avoid command substitution to keep the
# array mutations in the parent shell (bash 3.2 subshells can't write back).
VARIANT_IDX=0
variant_index() {
    local v="$1"
    local i=0
    for n in ${VAR_NAMES[@]+"${VAR_NAMES[@]}"}; do
        if [ "$n" = "$v" ]; then VARIANT_IDX=$i; return; fi
        i=$((i + 1))
    done
    VAR_NAMES+=("$v")
    VAR_PASS+=(0)
    VAR_FAIL+=(0)
    VARIANT_IDX=$((${#VAR_NAMES[@]} - 1))
}

variant_inc_pass() {
    variant_index "$1"
    VAR_PASS[$VARIANT_IDX]=$((${VAR_PASS[$VARIANT_IDX]} + 1))
}
variant_inc_fail() {
    variant_index "$1"
    VAR_FAIL[$VARIANT_IDX]=$((${VAR_FAIL[$VARIANT_IDX]} + 1))
}

SUMMARY_LINES=()

run_variant() {
    local variant="$1"
    local image="zstd-nginx-test:${variant}"
    local cname="zstd-run-${variant}-$$"
    local var_log_dir="${LOG_DIR}/${variant}"
    mkdir -p "$var_log_dir"

    if ! docker image inspect "$image" >/dev/null 2>&1; then
        echo "run.sh: image ${image} not found — run t/build.sh ${variant} first" >&2
        variant_inc_fail "$variant"
        SUMMARY_LINES+=("${variant}/<image-missing>: fail")
        return
    fi

    # Defensive: nuke any stale container under the same name, plus any
    # orphaned zstd-run-* containers that might still be hogging the host port
    # from a prior aborted invocation.
    docker rm -f "$cname" >/dev/null 2>&1 || true
    local orphan
    for orphan in $(docker ps -q --filter "name=zstd-run-${variant}-"); do
        docker rm -f "$orphan" >/dev/null 2>&1 || true
    done

    # Override the image's CMD with `sleep infinity` so the container stays
    # alive while individual regression scripts start/stop nginx themselves.
    # If we let the image's `CMD ["nginx"]` (daemon off, PID 1) run, the very
    # first `nginx -s stop` from a regression script would terminate PID 1 and
    # collapse the container.
    local cid
    # Mount the host-side regression scripts at /opt/regression AND the
    # current nginx.conf.template so iterating on either doesn't require
    # rebuilding the Docker image. The image's own COPYs of these paths are
    # shadowed by the bind mounts.
    if ! cid="$(docker run -d --rm \
            --platform "${ZSTD_TEST_PLATFORM:-linux/amd64}" \
            --name "$cname" \
            -p "${ZSTD_TEST_PORT}:8080" \
            -v "${REPO_ROOT}/t/regression:/opt/regression:ro" \
            -v "${REPO_ROOT}/t/docker/nginx.conf.template:/etc/nginx/templates/nginx.conf.template:ro" \
            --entrypoint sleep \
            "$image" \
            infinity \
            2>/tmp/docker-run.err)"; then
        echo "run.sh: docker run failed for ${variant}:" >&2
        cat /tmp/docker-run.err >&2 || true
        variant_inc_fail "$variant"
        SUMMARY_LINES+=("${variant}/<docker-run>: fail")
        return
    fi

    # Re-render /etc/nginx/nginx.conf from the (mounted) template so the
    # baseline reflects the current host-side template — not the version baked
    # into the image at build time. Variant-specific load_module / brotli
    # toggles mirror what the Dockerfile would have produced.
    local render_load='load_module modules/ngx_http_zstd_filter_module.so;'
    local render_extra=''
    case "$variant" in
        ubuntu-24.04-brotli)
            render_load=''
            render_extra='brotli on; brotli_min_length 0; brotli_types *;'
            ;;
    esac
    docker exec "$cid" sh -c "sed \
        -e 's|__LOAD_MODULES__|${render_load}|' \
        -e 's|__EXTRA_DIRECTIVES__|${render_extra}|' \
        -e 's|__EXTRA_LOCATIONS__||' \
        -e 's|__SERVER_PORT__|8080|' \
        /etc/nginx/templates/nginx.conf.template > /etc/nginx/nginx.conf"
    docker exec "$cid" sed -i 's|^daemon off;|daemon on;|' /etc/nginx/nginx.conf >/dev/null 2>&1 || true
    docker exec "$cid" nginx -c /etc/nginx/nginx.conf >/tmp/nginx-start.err 2>&1 || true

    local i=0
    while ! curl -fsS --max-time 1 "http://127.0.0.1:${ZSTD_TEST_PORT}/" >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -ge 60 ]; then
            echo "run.sh: container ${variant} did not listen on ${ZSTD_TEST_PORT} within 30s" >&2
            docker exec "$cid" cat /tmp/nginx-start.err >&2 2>/dev/null || true
            docker logs "$cid" >&2 || true
            docker stop "$cid" >/dev/null 2>&1 || true
            variant_inc_fail "$variant"
            SUMMARY_LINES+=("${variant}/baseline: fail (no listen)")
            return
        fi
        sleep 0.5
    done

    # Baseline liveness probe.
    local body status
    body="$(curl -fsS --max-time 5 "http://127.0.0.1:${ZSTD_TEST_PORT}/" 2>/dev/null || true)"
    status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${ZSTD_TEST_PORT}/")"
    if [ "$status" != "200" ] || [ "$(printf '%s' "$body" | tr -d '\n')" != "ok" ]; then
        echo "run.sh: ${variant} baseline failed (status=${status} body=[${body}])" >&2
        docker logs "$cid" >&2 || true
        docker stop "$cid" >/dev/null 2>&1 || true
        variant_inc_fail "$variant"
        SUMMARY_LINES+=("${variant}/baseline: fail")
        return
    fi
    SUMMARY_LINES+=("${variant}/baseline: pass")
    variant_inc_pass "$variant"

    # Stop the driver-launched nginx so individual scripts can re-render config.
    # Pass `-c` explicitly — the static-build (brotli) nginx defaults to the
    # built-in prefix /usr/local/nginx/conf/nginx.conf, which doesn't exist.
    docker exec "$cid" nginx -c /etc/nginx/nginx.conf -s stop >/dev/null 2>&1 || true
    i=0
    while docker exec "$cid" test -f /tmp/nginx.pid >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -ge 30 ]; then break; fi
        sleep 0.1
    done

    for script in ${REG_SCRIPTS[@]+"${REG_SCRIPTS[@]}"}; do
        if ! script_applies "$script" "$variant"; then
            SUMMARY_LINES+=("${variant}/${script}: skip")
            continue
        fi
        local log="${var_log_dir}/${script}.log"
        echo "  -> ${variant} :: ${script}"
        if docker exec "$cid" bash "/opt/regression/${script}" \
                > "$log" 2>&1; then
            SUMMARY_LINES+=("${variant}/${script}: pass")
            variant_inc_pass "$variant"
        else
            SUMMARY_LINES+=("${variant}/${script}: fail (log: ${log#${REPO_ROOT}/})")
            variant_inc_fail "$variant"
            echo "     --- tail ${log#${REPO_ROOT}/} ---"
            tail -n 30 "$log" | sed 's/^/     /'
            echo "     --- end tail ---"
        fi
    done

    docker stop "$cid" >/dev/null 2>&1 || true
}

for v in "${VARIANTS[@]}"; do
    echo "==> ${v}"
    run_variant "$v"
done

echo
echo "=== run summary ==="
for line in ${SUMMARY_LINES[@]+"${SUMMARY_LINES[@]}"}; do
    echo "  ${line}"
done
echo
echo "=== per-variant totals ==="
TOTAL_FAIL=0
i=0
for v in ${VAR_NAMES[@]+"${VAR_NAMES[@]}"}; do
    p=${VAR_PASS[$i]}
    f=${VAR_FAIL[$i]}
    TOTAL_FAIL=$((TOTAL_FAIL + f))
    printf '  %-32s pass=%d fail=%d\n' "$v" "$p" "$f"
    i=$((i + 1))
done

if [ "$TOTAL_FAIL" -gt 0 ]; then
    exit 1
fi
