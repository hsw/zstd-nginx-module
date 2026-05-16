#!/bin/bash
# sast.sh — host-side driver for the SAST docker variant.
#
# Builds `zstd-nginx-sast` if missing, then runs the in-container dispatcher.
# Output lands in t/sast-results/ on the host (bind-mounted).
#
# Usage:
#   bash t/sast.sh                  # build + run all 5 tools
#   bash t/sast.sh cppcheck         # run a single tool (still builds image)
#   bash t/sast.sh summary          # only re-print the saved summary
#
# Tools: scan-build | clang-tidy | cppcheck | gcc-fanalyzer | flawfinder | all | summary
#
# This driver mirrors the t/build.sh + t/run.sh pattern used by the runtime
# variants. Findings are advisory — V1 ships without fixing them. Triaging
# noise from real bugs is V2 work.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

TOOL="${1:-all}"
case "$TOOL" in
    scan-build|clang-tidy|cppcheck|gcc-fanalyzer|flawfinder|all|summary) ;;
    *)
        echo "sast.sh: unknown tool '${TOOL}'" >&2
        echo "  expected: scan-build | clang-tidy | cppcheck | gcc-fanalyzer | flawfinder | all | summary" >&2
        exit 2
        ;;
esac

IMAGE="zstd-nginx-sast:latest"
PLATFORM="${ZSTD_TEST_PLATFORM:-linux/amd64}"
RESULTS_DIR="${REPO_ROOT}/t/sast-results"
mkdir -p "$RESULTS_DIR"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "==> building ${IMAGE} from t/docker/Dockerfile.sast (--platform ${PLATFORM})"
    docker build \
        --platform "$PLATFORM" \
        -t "$IMAGE" \
        -f t/docker/Dockerfile.sast \
        . || {
        echo "sast.sh: docker build failed" >&2
        exit 1
    }
fi

echo "==> running SAST tool '${TOOL}' (results -> t/sast-results/)"
docker run --rm \
    --platform "$PLATFORM" \
    -v "${RESULTS_DIR}:/work/sast-results" \
    "$IMAGE" \
    "$TOOL"

rc=$?
echo
echo "=== sast summary ==="
echo "  tool:    ${TOOL}"
echo "  results: ${RESULTS_DIR}/"
ls -la "$RESULTS_DIR" 2>/dev/null | sed 's/^/    /' || true

exit "$rc"
