#!/bin/bash
# build.sh — build one or all docker test images (linux/amd64).
#
# Usage:
#   bash t/build.sh                     # build all 5 variants
#   bash t/build.sh ubuntu-24.04        # build just one
#   bash t/build.sh ubuntu-22.04 ubuntu-24.04-brotli  # subset
#
# Each variant builds an image tagged `zstd-nginx-test:<variant>`. Builds are
# idempotent (docker layer cache reused). Build context is the repo root so
# Dockerfiles can COPY the module source and t/docker/ helpers.
#
# Variant slugs MUST match Dockerfile filenames: t/docker/Dockerfile.<slug>.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

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

PLATFORM="${ZSTD_TEST_PLATFORM:-linux/amd64}"

declare -a OK_LIST FAIL_LIST
OK_LIST=()
FAIL_LIST=()

for v in "${VARIANTS[@]}"; do
    dockerfile="t/docker/Dockerfile.${v}"
    if [ ! -f "$dockerfile" ]; then
        echo "build.sh: no Dockerfile for variant '${v}' at ${dockerfile}" >&2
        FAIL_LIST+=("${v} (no Dockerfile)")
        continue
    fi

    image="zstd-nginx-test:${v}"
    echo "==> building ${image} from ${dockerfile} (--platform ${PLATFORM})"
    if docker build \
            --platform "$PLATFORM" \
            -t "$image" \
            -f "$dockerfile" \
            .; then
        OK_LIST+=("$v")
    else
        echo "build.sh: docker build failed for ${v}" >&2
        FAIL_LIST+=("$v")
    fi
done

echo
echo "=== build summary ==="
# ${arr[@]+...} guards against `set -u` tripping on an empty array on bash 4.x
# (the @-with-empty case is a long-standing bash quirk).
for v in ${OK_LIST[@]+"${OK_LIST[@]}"}; do echo "  ok    ${v}"; done
for v in ${FAIL_LIST[@]+"${FAIL_LIST[@]}"}; do echo "  fail  ${v}"; done

if [ "${#FAIL_LIST[@]}" -gt 0 ]; then
    exit 1
fi
