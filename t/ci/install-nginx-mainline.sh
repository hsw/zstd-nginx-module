#!/bin/bash
# install-nginx-mainline.sh — bootstrap nginx.org mainline apt repo and install
# nginx-dev (the headers + pkg-config bits needed to compile our dynamic modules
# out-of-tree).
#
# Usage:
#   install-nginx-mainline.sh [<nginx_version>]
#
#   nginx_version  Optional, e.g. "1.29.5". If omitted, installs the latest
#                  mainline available from the nginx.org apt repo.
#
# Designed to run as root inside a vanilla ubuntu:{22.04,24.04,26.04} container.
# Idempotent: re-running on a host where the key/sources/package are already
# present is a no-op (apt-get treats already-installed packages cleanly).
#
# Why factored out of Dockerfile.dynamic: the same bootstrap is needed by the
# CI build-deb job (which runs inside a vanilla ubuntu:<distro> container, not
# our pre-baked test image). Keeping one copy avoids drift between the test
# image and the package build environment.

set -euo pipefail

NGINX_VERSION="${1:-}"

# 1. Bootstrap deps. apt-get install is a no-op if already present, so this
#    is safe to re-run.
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg

# 2. Fetch and install the nginx.org signing key into the trusted-keyring
#    directory (signed-by approach, modern apt convention — keeps the key
#    scoped to this one source rather than world-trusted).
KEYRING=/usr/share/keyrings/nginx-archive-keyring.gpg
curl -fsSL https://nginx.org/keys/nginx_signing.key \
    | gpg --dearmor --yes -o "$KEYRING"

# 3. Detect Ubuntu codename (jammy / noble / future). /etc/os-release is
#    standard on every modern Ubuntu base image; no lsb-release dep needed.
# shellcheck source=/dev/null
. /etc/os-release
CODENAME="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
if [ -z "$CODENAME" ]; then
    echo "install-nginx-mainline.sh: cannot detect Ubuntu codename from /etc/os-release" >&2
    exit 1
fi

# 4. Write the nginx.org sources list. Overwrite unconditionally — cheap and
#    keeps the file canonical across re-runs.
echo "deb [signed-by=${KEYRING}] https://nginx.org/packages/mainline/ubuntu ${CODENAME} nginx" \
    > /etc/apt/sources.list.d/nginx.list

# 5. Refresh apt metadata against the new source.
apt-get update

# 6. Install nginx-dev. If a version was requested, pin it via apt's version
#    spec syntax. The trailing `-*` glob matches the nginx.org apt
#    upstream-revision suffix (e.g. `1.29.5-1~jammy`, `1.29.5-2~noble`) so
#    callers can pass the bare upstream version `1.29.5` without having to
#    know the per-distro revision string. Without the wildcard, apt rejects
#    the spec because the candidate package version on disk has the suffix
#    appended. Otherwise (no version requested) grab whatever the apt source
#    currently advertises as the candidate.
if [ -n "$NGINX_VERSION" ]; then
    apt-get install -y --no-install-recommends "nginx-dev=${NGINX_VERSION}-*"
else
    apt-get install -y --no-install-recommends nginx-dev
fi
