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

# Sanitizer findings are written to stderr (collected in the per-script log)
# rather than to per-pid files. The per-pid file approach (log_path=…) is
# fragile across nginx's master/worker fork: the worker inherits the path
# but writes from a different uid than the dir's owner, producing EACCES.
# Stderr capture sidesteps that and keeps findings co-located with the
# regression-script trace anyway.

# Regression set. h2-truncation needs http_v2 (not compiled into this build);
# filter-priority needs the brotli module (not present). Everything else is
# in scope.
SCRIPTS=(
    accept-encoding.sh
    dict-reload.sh
    head-parity.sh
    infinite-loop.sh
    new-directives.sh
)

overall_rc=0
for script in "${SCRIPTS[@]}"; do
    log="${LOG_DIR}/${script}.log"
    echo "==> asan :: ${script}"
    # See Dockerfile.asan ENV block for rationale. The key bits:
    #   * detect_leaks=0 — nginx cycle pool isn't a leak.
    #   * exitcode=0 — let workers keep serving after a finding (the well-
    #     known ngx_output_chain UB would otherwise crash every worker on
    #     every request).
    # Findings still show up as `runtime error:` / `==ERROR:` lines in stderr,
    # which the driver greps for below.
    if docker exec \
            -e ASAN_OPTIONS="detect_leaks=0:abort_on_error=0:exitcode=0:halt_on_error=0:strict_string_checks=1:check_initialization_order=1:print_stacktrace=1" \
            -e UBSAN_OPTIONS="print_stacktrace=1:halt_on_error=0:exitcode=0" \
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
