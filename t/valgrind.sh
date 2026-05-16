#!/bin/bash
# valgrind.sh — host-side driver for the Valgrind memcheck variant.
#
# Builds `zstd-nginx-valgrind` if missing, starts it detached, runs a short
# regression subset under valgrind, and reports any "definitely lost" or
# "indirectly lost" findings.
#
# Usage:
#   bash t/valgrind.sh                   # full default flow
#
# Valgrind imposes ~50× slowdown so we only run two regression scripts
# (accept-encoding + dict-reload). Full coverage runs against the regular
# runtime variants via t/run.sh.
#
# Output logs land in t/valgrind-logs/ on the host.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

IMAGE="zstd-nginx-valgrind:latest"
CNAME="zstd-valgrind-$$"
PLATFORM="${ZSTD_TEST_PLATFORM:-linux/amd64}"
PORT="${ZSTD_TEST_PORT:-8080}"
LOG_DIR="${REPO_ROOT}/t/valgrind-logs"
mkdir -p "$LOG_DIR"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "==> building ${IMAGE} from t/docker/Dockerfile.valgrind (--platform ${PLATFORM})"
    docker build \
        --platform "$PLATFORM" \
        -t "$IMAGE" \
        -f t/docker/Dockerfile.valgrind \
        . || {
        echo "valgrind.sh: docker build failed" >&2
        exit 1
    }
fi

# Defensive: clean up stale containers from prior aborted runs.
docker rm -f "$CNAME" >/dev/null 2>&1 || true
for orphan in $(docker ps -q --filter "name=zstd-valgrind-"); do
    docker rm -f "$orphan" >/dev/null 2>&1 || true
done

# Start the container with `sleep infinity` so we can exec individual
# regression scripts (each one starts/stops nginx itself).
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
    echo "valgrind.sh: docker run failed" >&2
    exit 1
}

cleanup() {
    docker stop "$cid" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Wrap nginx under valgrind. We do this by replacing /usr/sbin/nginx with a
# shell wrapper that exec's valgrind. The regression scripts invoke
# `nginx -c ... -t` and `nginx -c ...` directly, so wrapping the binary
# means every invocation goes through valgrind.
#
# --trace-children=yes is critical: without it valgrind only follows the
# original master process. With master_process off (set by ZSTD_REGRESSION_NO_DAEMON
# below, in _common.sh's apply_daemon_mode) there are no children so the flag
# is technically redundant, but we keep it so a hand-spawned background
# child (e.g. python fixture servers in infinite-loop.sh) and any future
# split master/worker test still get coverage.
docker exec "$cid" sh -c 'mv /usr/sbin/nginx /usr/sbin/nginx.real && cat > /usr/sbin/nginx << "EOF"
#!/bin/sh
# valgrind wrapper: every `nginx ...` call goes through memcheck.
exec valgrind \
    --tool=memcheck \
    --leak-check=full \
    --show-leak-kinds=definite,indirect \
    --track-origins=yes \
    --trace-children=yes \
    --error-exitcode=99 \
    --log-file=/tmp/valgrind.%p.log \
    --suppressions=/etc/valgrind.supp \
    /usr/sbin/nginx.real "$@"
EOF
chmod +x /usr/sbin/nginx'

# Regression subset. accept-encoding exercises the compression hot-path under
# memcheck and is the highest-leverage script for catching real leaks in the
# per-request path. dict-reload is excluded — its core assertion is "master
# RSS does not grow across N nginx -s reload cycles", which is incompatible
# with the single-process-foreground mode we force under valgrind (no master,
# so SIGHUP semantics differ and the reload loop never completes).
SCRIPTS=(accept-encoding.sh)

overall_rc=0
for script in "${SCRIPTS[@]}"; do
    log="${LOG_DIR}/${script}.log"
    echo "==> valgrind :: ${script}"
    # ZSTD_REGRESSION_NO_DAEMON=1 keeps nginx in foreground + single-process
    # mode under valgrind. The default daemonize path double-forks the worker
    # away from the valgrind-traced master, so the request hot-path runs
    # OUTSIDE memcheck. See _common.sh::apply_daemon_mode for what that flag
    # actually flips.
    if docker exec \
            -e ZSTD_REGRESSION_NO_DAEMON=1 \
            "$cid" bash "/opt/regression/${script}" \
            > "$log" 2>&1; then
        echo "  pass  ${script} (log: t/valgrind-logs/${script}.log)"
    else
        rc=$?
        echo "  fail  ${script} rc=${rc} (log: t/valgrind-logs/${script}.log)"
        overall_rc=1
    fi
done

# Pull valgrind logs out of the container. They were written via --log-file=
# above, one per nginx pid. Grep for actionable findings.
echo
echo "==> collecting valgrind logs"
mkdir -p "${LOG_DIR}/raw"
docker exec "$cid" sh -c 'ls /tmp/valgrind.*.log 2>/dev/null || true' \
    | while IFS= read -r f; do
        [ -n "$f" ] || continue
        out="${LOG_DIR}/raw/$(basename "$f")"
        docker cp "${cid}:${f}" "$out" >/dev/null 2>&1 || true
    done

# Scan for:
#   * "definitely lost" with nonzero byte counts (real leaks)
#   * Any memcheck error class: Invalid read, Invalid write, use of
#     uninitialised value, mismatched free/delete, etc. (these surface
#     as "ERROR SUMMARY: N errors" with N>0).
# We ignore "indirectly lost" reports on their own: an indirectly-lost block
# is, by valgrind's definition, reachable from another lost-or-reachable
# allocation. When that root is a known/suppressed nginx-core init pool
# ("free everything at process exit"), valgrind STILL reports the indirect
# chain even though the supp suppresses the root. For full breakdown, read
# the per-pid logs in t/valgrind-logs/raw/.
echo
echo "=== valgrind findings ==="
found_real=0
for f in "${LOG_DIR}/raw"/*.log; do
    [ -f "$f" ] || continue
    if grep -E 'definitely lost: [1-9]' "$f" >/dev/null 2>&1 \
       || grep -E 'ERROR SUMMARY: [1-9]' "$f" >/dev/null 2>&1; then
        echo "--- ${f#${REPO_ROOT}/} ---"
        grep -E 'definitely lost:|indirectly lost:|possibly lost:|still reachable:|ERROR SUMMARY:|Invalid (read|write)|Conditional jump|Use of uninitialised' "$f" | head -12
        found_real=1
    fi
done

if [ "$found_real" -eq 0 ]; then
    echo "  no leaks beyond suppressions"
else
    overall_rc=1
fi

echo
echo "=== valgrind summary ==="
echo "  regression scripts: ${SCRIPTS[*]}"
echo "  log dir:            ${LOG_DIR}/"

exit "$overall_rc"
