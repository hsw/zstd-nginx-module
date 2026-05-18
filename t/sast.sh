#!/bin/bash
# sast.sh — host-side driver for the parallel SAST + Valgrind suite.
#
# Mirrors nginx-ssl-fingerprint's tmp/docker/docker-compose.sast.yml pattern:
# 5 SAST tools (scan-build, clang-tidy, cppcheck, gcc-fanalyzer, flawfinder)
# run in parallel containers from the same image; valgrind runs alongside
# from its own image. Outputs land in tmp/sast-results/ and tmp/valgrind-logs/
# on the host.
#
# Usage:
#   bash t/sast.sh                  # all 6 (5 SAST + valgrind) in parallel, then summary
#   bash t/sast.sh scan-build       # single tool
#   bash t/sast.sh clang-tidy
#   bash t/sast.sh cppcheck
#   bash t/sast.sh gcc-fanalyzer
#   bash t/sast.sh flawfinder
#   bash t/sast.sh valgrind
#   bash t/sast.sh sast             # only the 5 SAST tools (no valgrind)
#   bash t/sast.sh summary          # re-print saved SAST summary
#
# Findings are advisory; this driver returns nonzero if compose reports a
# non-zero exit from any service (so CI can gate on it).

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

TOOL="${1:-all}"
case "$TOOL" in
    scan-build|clang-tidy|cppcheck|gcc-fanalyzer|flawfinder|valgrind|all|sast|summary) ;;
    *)
        echo "sast.sh: unknown tool '${TOOL}'" >&2
        echo "  expected: scan-build | clang-tidy | cppcheck | gcc-fanalyzer | flawfinder | valgrind | all | sast | summary" >&2
        exit 2
        ;;
esac

COMPOSE_FILE="t/docker/docker-compose.sast.yml"
mkdir -p "${REPO_ROOT}/tmp/sast-results" "${REPO_ROOT}/tmp/valgrind-logs"

# Two-step pattern: build first (deduped per unique image), then run in
# parallel. Avoids the first-run race where multiple services with the same
# image: name kick off redundant build context transfers before BuildKit
# realises they're identical. On warm cache this is a fast no-op.
build_images() {
    local services=("$@")
    echo "==> building images for: ${services[*]}"
    docker compose -f "$COMPOSE_FILE" build "${services[@]}"
}

# Map service name -> container name (per docker-compose.sast.yml).
container_name() {
    case "$1" in
        scan-build|clang-tidy|cppcheck|gcc-fanalyzer|flawfinder|valgrind|summary) echo "sast-$1" ;;
        *) echo "" ;;
    esac
}

# Run a set of services in parallel and collect their exit codes after all
# finish. Avoids --abort-on-container-exit, which SIGKILLs longer-running
# tools as soon as the fastest one (flawfinder) finishes.
run_parallel() {
    local services=("$@")
    docker compose -f "$COMPOSE_FILE" up "${services[@]}"
    local rc=0
    echo
    echo "==> per-service exit codes"
    for s in "${services[@]}"; do
        local cname
        cname="$(container_name "$s")"
        local code
        code="$(docker inspect --format '{{.State.ExitCode}}' "$cname" 2>/dev/null || echo "missing")"
        printf "  %-16s exit=%s\n" "$s" "$code"
        if [ "$code" != "0" ] && [ "$code" != "missing" ]; then
            rc=1
        fi
    done
    return $rc
}

rc=0
case "$TOOL" in
    all)
        build_images scan-build clang-tidy cppcheck gcc-fanalyzer flawfinder valgrind
        echo "==> running all SAST tools + valgrind in parallel (results -> tmp/sast-results/ + tmp/valgrind-logs/)"
        run_parallel scan-build clang-tidy cppcheck gcc-fanalyzer flawfinder valgrind
        rc=$?
        echo
        echo "==> SAST summary"
        docker compose -f "$COMPOSE_FILE" run --rm summary || true
        ;;
    sast)
        build_images scan-build clang-tidy cppcheck gcc-fanalyzer flawfinder
        echo "==> running 5 SAST tools in parallel (no valgrind)"
        run_parallel scan-build clang-tidy cppcheck gcc-fanalyzer flawfinder
        rc=$?
        echo
        echo "==> SAST summary"
        docker compose -f "$COMPOSE_FILE" run --rm summary || true
        ;;
    summary)
        docker compose -f "$COMPOSE_FILE" run --rm summary
        rc=$?
        ;;
    *)
        build_images "$TOOL"
        echo "==> running single service: ${TOOL}"
        run_parallel "$TOOL"
        rc=$?
        ;;
esac

echo
echo "=== sast.sh summary ==="
echo "  tool:            ${TOOL}"
echo "  sast results:    ${REPO_ROOT}/tmp/sast-results/"
echo "  valgrind logs:   ${REPO_ROOT}/tmp/valgrind-logs/"
ls -la "${REPO_ROOT}/tmp/sast-results" 2>/dev/null | sed 's/^/    /' | head -15 || true

exit "$rc"
