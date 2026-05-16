#!/bin/bash
# coverage.sh — host driver for the gcov-instrumented variant.
#
# Builds zstd-nginx-coverage if missing, runs every regression script the
# image can support, then asks gcov inside the container to summarise the
# resulting .gcda files. Output:
#   * Per-file gcov line-coverage % (filter and static modules).
#   * t/coverage-logs/uncovered.txt — every source line with execution count 0.
#   * t/coverage-logs/<script>.log — stdout/stderr of each regression run.
#
# Caveats:
#   * `nginx -s reload` doesn't flush .gcda from the OLD worker reliably (the
#     worker exits via _exit() under reload semantics). dict-reload.sh is
#     included so the master path is still covered, but the per-reload-cycle
#     worker code path is undercounted by gcov.
#   * Scripts that rely on ZSTD_REGRESSION_NO_DAEMON for sanitizer-friendly
#     stderr inheritance run fine here too; we don't need that flag because
#     the master gracefully shuts down and flushes .gcda on `nginx -s stop`.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

IMAGE="zstd-nginx-coverage:latest"
CNAME="zstd-cov-$$"
PLATFORM="${ZSTD_TEST_PLATFORM:-linux/amd64}"
PORT="${ZSTD_TEST_PORT:-8080}"
LOG_DIR="${REPO_ROOT}/tmp/coverage-logs"
mkdir -p "$LOG_DIR"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "==> building ${IMAGE} from t/docker/Dockerfile.coverage (--platform ${PLATFORM})"
    docker build \
        --platform "$PLATFORM" \
        -t "$IMAGE" \
        -f t/docker/Dockerfile.coverage \
        . || {
        echo "coverage.sh: docker build failed" >&2
        exit 1
    }
fi

docker rm -f "$CNAME" >/dev/null 2>&1 || true
for orphan in $(docker ps -q --filter "name=zstd-cov-"); do
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
    echo "coverage.sh: docker run failed" >&2
    exit 1
}

cleanup() {
    docker stop "$cid" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# All regression scripts ported to pytest (test_*.py). filter-priority.sh
# stays as bash because brotli isn't built into the coverage image.
overall_rc=0

# Pytest pass: runs in the same container so .gcda accumulators capture
# every executed branch. Coverage container is daemon-on (no NO_DAEMON
# override) so pytest's nginx fixture starts/stops a real master+worker.
echo "==> coverage :: pytest"
pytest_log="${LOG_DIR}/pytest.log"
junit_xml="${LOG_DIR}/pytest-junit.xml"
if docker exec "$cid" bash -c "cd /opt/regression && python3 -m pytest -v --tb=short --color=no -p no:cacheprovider --junitxml=/tmp/pytest-junit.xml 2>&1" \
        > "$pytest_log" 2>&1; then
    echo "  pass  pytest"
else
    rc=$?
    echo "  fail  pytest rc=${rc} (log: t/coverage-logs/pytest.log)"
    overall_rc=1
fi
docker cp "$cid:/tmp/pytest-junit.xml" "$junit_xml" >/dev/null 2>&1 || true

# Run gcov against the instrumented sources. The .gcno / .gcda files live in
# /usr/local/src/nginx/objs/addon/{filter,static}/. gcov needs the source
# location (filter/*.c, static/*.c) and the object directory containing the
# matching .gcno + .gcda. -b enables branch coverage, -c shows counts.
echo
echo "=== gcov summary ==="
docker exec "$cid" bash -c '
    set -u
    cd /usr/local/src/nginx
    gcov -b -c \
        -o objs/addon/filter \
        /src/filter/ngx_http_zstd_filter_module.c 2>/dev/null \
        | grep -E "^(File|Lines|Branches|No branches|Calls)" | head -8
    echo
    gcov -b -c \
        -o objs/addon/static \
        /src/static/ngx_http_zstd_static_module.c 2>/dev/null \
        | grep -E "^(File|Lines|Branches|No branches|Calls)" | head -8
'

# Pull .gcov annotations out so we can show uncovered lines on the host.
docker exec "$cid" bash -c '
    cd /usr/local/src/nginx
    gcov -b -c -o objs/addon/filter /src/filter/ngx_http_zstd_filter_module.c >/dev/null 2>&1
    gcov -b -c -o objs/addon/static /src/static/ngx_http_zstd_static_module.c >/dev/null 2>&1
    # gcov writes .gcov files into cwd by default.
    ls -1 *.gcov 2>/dev/null
' > "${LOG_DIR}/gcov-files.txt"

# Extract executable but never-executed lines (####### marker). We deliberately
# skip header-included gcov files (.h.gcov) — they pull in nginx-core noise.
echo
echo "=== uncovered lines (first 80) ==="
docker exec "$cid" bash -c '
    cd /usr/local/src/nginx
    for f in ngx_http_zstd_filter_module.c.gcov ngx_http_zstd_static_module.c.gcov; do
        if [ -f "$f" ]; then
            echo "--- $f ---"
            grep -nE "^[[:space:]]*#####:" "$f" | head -200
        fi
    done
' | tee "${LOG_DIR}/uncovered.txt" | head -120

echo
echo "=== coverage summary ==="
echo "  log dir:           ${LOG_DIR}/"
echo "  uncovered detail:  t/coverage-logs/uncovered.txt"

exit "$overall_rc"
