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
# Variant slug → (Dockerfile, build args). The dynamic variants share one
# Dockerfile (`Dockerfile.dynamic`) parameterised by UBUNTU_VERSION + VARIANT;
# the brotli variant has its own Dockerfile because it's a static build with
# extra deps (ngx_brotli).

set -euo pipefail

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
# angie-{22.04,24.04,26.04} variants exist as on-demand targets in the
# case-dispatch below — built explicitly via `bash t/build.sh angie-24.04`
# or similar. Excluded from ALL_VARIANTS because they reproduce the same
# bug signature as the matching ubuntu-NN.NN variant (nginx-core 1.29.3
# code path, inherited unchanged by angie 1.11.5) — adding them to the
# default matrix doubles CI cost for zero unique signal.

if [ "$#" -gt 0 ]; then
    VARIANTS=("$@")
else
    VARIANTS=("${ALL_VARIANTS[@]}")
fi

# Empty = let docker pick the host's native architecture (M1/M2/M3 → arm64,
# x86_64 Linux runners → amd64, etc). Set ZSTD_TEST_PLATFORM=linux/amd64 in
# CI when production-parity arch is required even on arm64 runners.
PLATFORM="${ZSTD_TEST_PLATFORM:-}"
# Conditional --platform flag: emits nothing when PLATFORM is empty,
# so docker uses native. Avoids "--platform=" empty-value issues.
PLATFORM_FLAG=()
[ -n "$PLATFORM" ] && PLATFORM_FLAG=(--platform "$PLATFORM")

declare -a OK_LIST FAIL_LIST
OK_LIST=()
FAIL_LIST=()

for v in "${VARIANTS[@]}"; do
    case "$v" in
        ubuntu-22.04)
            dockerfile="t/docker/Dockerfile.dynamic"
            build_args=(--build-arg UBUNTU_VERSION=22.04 --build-arg VARIANT=default)
            ;;
        ubuntu-24.04)
            dockerfile="t/docker/Dockerfile.dynamic"
            build_args=(--build-arg UBUNTU_VERSION=24.04 --build-arg VARIANT=default)
            ;;
        ubuntu-24.04-shared-only)
            dockerfile="t/docker/Dockerfile.dynamic"
            build_args=(--build-arg UBUNTU_VERSION=24.04 --build-arg VARIANT=shared-only)
            ;;
        ubuntu-26.04)
            dockerfile="t/docker/Dockerfile.dynamic"
            build_args=(--build-arg UBUNTU_VERSION=26.04 --build-arg VARIANT=default)
            ;;
        ubuntu-24.04-brotli)
            dockerfile="t/docker/Dockerfile.brotli"
            build_args=(--build-arg UBUNTU_VERSION=24.04)
            ;;
        ubuntu-24.04-dynamic-brotli)
            dockerfile="t/docker/Dockerfile.dynamic-brotli"
            build_args=(--build-arg UBUNTU_VERSION=24.04)
            ;;
        angie-22.04)
            dockerfile="t/docker/Dockerfile.angie"
            build_args=(--build-arg UBUNTU_VERSION=22.04)
            ;;
        angie-24.04)
            dockerfile="t/docker/Dockerfile.angie"
            build_args=(--build-arg UBUNTU_VERSION=24.04)
            ;;
        angie-26.04)
            dockerfile="t/docker/Dockerfile.angie"
            build_args=(--build-arg UBUNTU_VERSION=26.04)
            ;;
        *)
            echo "build.sh: unknown variant '${v}'" >&2
            FAIL_LIST+=("${v} (unknown variant)")
            continue
            ;;
    esac

    if [ ! -f "$dockerfile" ]; then
        echo "build.sh: no Dockerfile at ${dockerfile}" >&2
        FAIL_LIST+=("${v} (no Dockerfile)")
        continue
    fi

    image="zstd-nginx-test:${v}"
    echo "==> building ${image} from ${dockerfile} (platform=${PLATFORM:-native} ${build_args[*]})"
    if docker build \
            ${PLATFORM_FLAG[@]+"${PLATFORM_FLAG[@]}"} \
            "${build_args[@]}" \
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
