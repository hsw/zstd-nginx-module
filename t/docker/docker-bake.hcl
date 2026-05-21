# docker-bake.hcl — multi-target build for the regression matrix.
#
# Run via: `docker buildx bake -f t/docker/docker-bake.hcl`
#
# BuildKit shares common base layers (apt-get update, nginx-mainline repo
# setup, python3-zstandard install) across all targets that derive from
# the same Dockerfile, so the parallel build is faster than 6× independent
# `docker build` invocations even with the standard layer cache.
#
# The dockerfile / args / tags mirror the case-dispatch in t/build.sh.
# Keep them in sync when adding new variants.

group "default" {
    targets = [
        "ubuntu-22-04",
        "ubuntu-24-04",
        "ubuntu-24-04-shared-only",
        "ubuntu-26-04",
        "ubuntu-24-04-brotli",
        "ubuntu-24-04-dynamic-brotli",
    ]
}

# Optional override: ZSTD_TEST_PLATFORM=linux/amd64 docker buildx bake ...
# Empty default lets buildx pick the host's native arch (Rosetta on Apple
# silicon → amd64 via Rosetta; native arm64 if you want it).
variable "PLATFORM" {
    default = ""
}

# Build context is the repo root. The HCL `context` is resolved relative to
# the current working directory at bake-invocation time, not relative to the
# HCL file — `t/matrix-parallel.sh` cd's to the repo root before running
# `docker buildx bake -f t/docker/docker-bake.hcl ...`, so a literal "."
# resolves correctly.
target "_common" {
    context    = "."
    platforms  = PLATFORM == "" ? null : [PLATFORM]
}

target "ubuntu-22-04" {
    inherits   = ["_common"]
    dockerfile = "t/docker/Dockerfile.dynamic"
    args = { UBUNTU_VERSION = "22.04", VARIANT = "default" }
    tags = ["zstd-nginx-test:ubuntu-22.04"]
}

target "ubuntu-24-04" {
    inherits   = ["_common"]
    dockerfile = "t/docker/Dockerfile.dynamic"
    args = { UBUNTU_VERSION = "24.04", VARIANT = "default" }
    tags = ["zstd-nginx-test:ubuntu-24.04"]
}

target "ubuntu-24-04-shared-only" {
    inherits   = ["_common"]
    dockerfile = "t/docker/Dockerfile.dynamic"
    args = { UBUNTU_VERSION = "24.04", VARIANT = "shared-only" }
    tags = ["zstd-nginx-test:ubuntu-24.04-shared-only"]
}

target "ubuntu-26-04" {
    inherits   = ["_common"]
    dockerfile = "t/docker/Dockerfile.dynamic"
    args = { UBUNTU_VERSION = "26.04", VARIANT = "default" }
    tags = ["zstd-nginx-test:ubuntu-26.04"]
}

target "ubuntu-24-04-brotli" {
    inherits   = ["_common"]
    dockerfile = "t/docker/Dockerfile.brotli"
    args = { UBUNTU_VERSION = "24.04" }
    tags = ["zstd-nginx-test:ubuntu-24.04-brotli"]
}

target "ubuntu-24-04-dynamic-brotli" {
    inherits   = ["_common"]
    dockerfile = "t/docker/Dockerfile.dynamic-brotli"
    args = { UBUNTU_VERSION = "24.04" }
    tags = ["zstd-nginx-test:ubuntu-24.04-dynamic-brotli"]
}
