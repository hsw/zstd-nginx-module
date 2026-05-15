#!/bin/bash
# asan.sh — host-side driver for the AddressSanitizer + UBSan variant.
#
# Builds `zstd-nginx-asan` if missing, starts it detached, runs the full
# regression suite inside the container, and reports any ASan/UBSan
# diagnostics that surface on nginx's stderr.
#
# Usage:
#   bash t/asan.sh                       # full default flow
#
# ASan instrumentation imposes ~3× slowdown — full regression run is fine.
# We exclude filter-priority.sh (it's brotli-only) and h2-truncation.sh
# (HTTP/2 wasn't compiled in — the build above doesn't ask for it).
#
# Output logs land in t/asan-logs/ on the host.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

IMAGE="zstd-nginx-asan:latest"
CNAME="zstd-asan-$$"
PLATFORM="${ZSTD_TEST_PLATFORM:-linux/amd64}"
PORT="${ZSTD_TEST_PORT:-8080}"
LOG_DIR="${REPO_ROOT}/t/asan-logs"
mkdir -p "$LOG_DIR"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "==> building ${IMAGE} from t/docker/Dockerfile.asan (--platform ${PLATFORM})"
    docker build \
        --platform "$PLATFORM" \
        -t "$IMAGE" \
        -f t/docker/Dockerfile.asan \
        . || {
        echo "asan.sh: docker build failed" >&2
        exit 1
    }
fi

docker rm -f "$CNAME" >/dev/null 2>&1 || true
for orphan in $(docker ps -q --filter "name=zstd-asan-"); do
    docker rm -f "$orphan" >/dev/null 2>&1 || true
done

echo "==> starting ${CNAME} (port ${PORT})"
cid="$(docker run -d --rm \
        --platform "$PLATFORM" \
        --name "$CNAME" \
        -p "${PORT}:8080" \
        -v "${REPO_ROOT}/t/regression:/opt/regression:ro" \
        -v "${REPO_ROOT}/t/docker/nginx.conf.template:/etc/nginx/templates/nginx.conf.template:ro" \
        --entrypoint sleep \
        "$IMAGE" \
        infinity)" || {
    echo "asan.sh: docker run failed" >&2
    exit 1
}

cleanup() {
    docker stop "$cid" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Sanitizer findings are written to stderr (collected in the per-script log).
# We force the regression scripts into single-process foreground mode
# (ZSTD_REGRESSION_NO_DAEMON=1) so the request-handling process inherits the
# stderr captured by `docker exec` — under daemonize, the worker double-forks
# away from the captured stderr and findings silently disappear into /dev/null.

# Regression set. filter-priority needs the brotli module (not present in the
# asan image); h2-truncation needs http_v2 which we now request in
# Dockerfile.asan so it can exercise the H2 hot-path under sanitizers.
# dict-reload is excluded: its core assertion is "master RSS does not grow
# across N nginx -s reload cycles". Reload semantics differ under
# ZSTD_REGRESSION_NO_DAEMON=1 (master_process off) where there is no master/
# worker handoff to measure, and ASan instrumentation adds enough non-leak
# RSS noise per cycle to blow past the 512 KiB threshold even when the CDict
# cleanup hook is wired up correctly. Leak coverage of the dict path lives in
# valgrind.sh instead.
SCRIPTS=(
    accept-encoding.sh
    h2-truncation.sh
    head-parity.sh
    infinite-loop.sh
    new-directives.sh
)

overall_rc=0
for script in "${SCRIPTS[@]}"; do
    log="${LOG_DIR}/${script}.log"
    echo "==> asan :: ${script}"
    # See Dockerfile.asan ENV block for rationale.
    # Sanitizer abort-on-error rationale: with abort_on_error=1 + halt_on_error=1
    # the very first finding aborts the worker, which (in single-process mode
    # set below) takes the listener with it — curl gets a connection error and
    # the regression script's `nginx -t` / nginx start path bails immediately.
    # This is the desired behaviour: a finding becomes a HARD test failure
    # rather than a stderr line the driver might miss with a soft grep. The
    # earlier `exitcode=0:halt_on_error=0` policy made findings advisory, which
    # missed UBSan reports under stderr-eating daemonize.
    #
    # detect_leaks=0 stays because the nginx cycle pool ("free everything on
    # exit") looks like a leak to ASan but isn't a defect.
    if docker exec \
            -e ZSTD_REGRESSION_NO_DAEMON=1 \
            -e ASAN_OPTIONS="detect_leaks=0:abort_on_error=1:halt_on_error=1:strict_string_checks=1:check_initialization_order=1:print_stacktrace=1" \
            -e UBSAN_OPTIONS="print_stacktrace=1:halt_on_error=1:abort_on_error=1:suppressions=/etc/ubsan.supp" \
            "$cid" bash "/opt/regression/${script}" \
            > "$log" 2>&1; then
        echo "  pass  ${script} (log: t/asan-logs/${script}.log)"
    else
        rc=$?
        echo "  fail  ${script} rc=${rc} (log: t/asan-logs/${script}.log)"
        overall_rc=1
    fi
done

# Scan regression stderr for ASan/UBSan markers. We deliberately filter out
# the two well-known nginx-core noise sources:
#   * ngx_output_chain.c:70 "incorrect function type" — nginx-core function-
#     pointer cast through void*-ctx; affects every module, not our code.
#   * LeakSanitizer reports — disabled in ASAN_OPTIONS above; if any slip
#     through here, the pattern below still strips them.
echo
echo "=== sanitizer findings ==="
found_real=0
for f in "${LOG_DIR}"/*.log; do
    [ -f "$f" ] || continue
    if grep -E '==ERROR: AddressSanitizer|runtime error:' "$f" 2>/dev/null \
            | grep -v 'ngx_output_chain.c:.*runtime error: call to function.*through pointer to incorrect function type' \
            | grep -v 'LeakSanitizer' \
            | grep -q .; then
        echo "--- ${f#${REPO_ROOT}/} ---"
        grep -E '==ERROR:|runtime error:|SUMMARY:' "$f" \
            | grep -v 'ngx_output_chain.c:.*runtime error: call to function.*through pointer to incorrect function type' \
            | grep -v 'undefined-behavior src/core/ngx_output_chain.c' \
            | grep -v 'LeakSanitizer' \
            | head -15
        found_real=1
    fi
done

if [ "$found_real" -eq 0 ]; then
    echo "  no ASan/UBSan diagnostics"
else
    overall_rc=1
fi

echo
echo "=== asan summary ==="
echo "  regression scripts: ${SCRIPTS[*]}"
echo "  log dir:            ${LOG_DIR}/"

exit "$overall_rc"
