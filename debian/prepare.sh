#!/bin/bash
# debian/prepare.sh — substitute nginx ABI-pin placeholders in
# debian/control.in to produce debian/control.
#
# Usage: debian/prepare.sh <nginx_version>
#   nginx_version  e.g. "1.31.2"
#
# Substitution rule (patch-exact ABI pin):
#   NGINX_VERSION_LOWER = <major>.<minor>.<patch>     (exact build version)
#   NGINX_VERSION_UPPER = <major>.<minor>.<patch+1>   (next patch release)
#
# Reads:  debian/control.in  (relative to CWD)
# Writes: debian/control
# Exits:  0 on success; 1 on usage/IO/format error.

set -euo pipefail

usage() {
    cat >&2 <<'EOF'
Usage: debian/prepare.sh <nginx_version>
  nginx_version  e.g. "1.31.2"

Reads  debian/control.in and writes debian/control with the
@NGINX_VERSION_LOWER@ / @NGINX_VERSION_UPPER@ placeholders substituted
according to the pkg-oss ABI-pin convention.
EOF
}

if [ "$#" -ne 1 ]; then
    usage
    exit 1
fi

nginx_version="$1"

# Strict format check: <digits>.<digits>.<digits>
if ! [[ "$nginx_version" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
    echo "error: nginx_version must match <major>.<minor>.<patch>, got: ${nginx_version}" >&2
    exit 1
fi

major="${BASH_REMATCH[1]}"
minor="${BASH_REMATCH[2]}"
# patch component pins the exact ABI — nginx core rejects any dynamic module
# whose compiled-in nginx_version differs from the running binary, so the pin
# is patch-exact: [patch, patch+1).

lower="${major}.${minor}.${BASH_REMATCH[3]}"
upper="${major}.${minor}.$((BASH_REMATCH[3] + 1))"

input="debian/control.in"
output="debian/control"

if [ ! -f "$input" ]; then
    echo "error: ${input} not found (run from repo root)" >&2
    exit 1
fi

# sed-based substitution; write atomically via a temp file in the same dir.
tmp=$(mktemp "${output}.XXXXXX")
trap 'rm -f "$tmp"' EXIT

sed -e "s/@NGINX_VERSION_LOWER@/${lower}/g" \
    -e "s/@NGINX_VERSION_UPPER@/${upper}/g" \
    "$input" > "$tmp"

mv "$tmp" "$output"
trap - EXIT
