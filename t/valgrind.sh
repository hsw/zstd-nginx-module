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
docker exec "$cid" sh -c 'mv /usr/sbin/nginx /usr/sbin/nginx.real && cat > /usr/sbin/nginx << "EOF"
#!/bin/sh
# valgrind wrapper: every `nginx ...` call goes through memcheck.
exec valgrind \
    --tool=memcheck \
    --leak-check=full \
    --show-leak-kinds=definite,indirect \
    --track-origins=yes \
    --error-exitcode=99 \
    --log-file=/tmp/valgrind.%p.log \
    --suppressions=/etc/valgrind.supp \
    /usr/sbin/nginx.real "$@"
EOF
chmod +x /usr/sbin/nginx'

# Regression subset. Both these scripts exercise the request hot-path
# (compression filter) and the reload path (CDict cleanup).
SCRIPTS=(accept-encoding.sh dict-reload.sh)

overall_rc=0
for script in "${SCRIPTS[@]}"; do
    log="${LOG_DIR}/${script}.log"
    echo "==> valgrind :: ${script}"
    if docker exec "$cid" bash "/opt/regression/${script}" \
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

# Scan for "definitely lost" + "indirectly lost" with nonzero byte counts.
echo
echo "=== valgrind findings ==="
found_real=0
for f in "${LOG_DIR}/raw"/*.log; do
    [ -f "$f" ] || continue
    if grep -E 'definitely lost: [1-9]|indirectly lost: [1-9]' "$f" >/dev/null 2>&1; then
        echo "--- ${f#${REPO_ROOT}/} ---"
        grep -E 'definitely lost:|indirectly lost:|possibly lost:|still reachable:' "$f" | head -8
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
