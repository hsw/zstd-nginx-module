#!/bin/bash
# build-module.sh — single source of truth for invoking nginx configure + make
# from inside a Dockerfile RUN step.
#
# The Dockerfile is responsible for:
#   1. Installing nginx from the nginx.org mainline apt repo so we get a known
#      upstream version (not the distro patchset).
#   2. Detecting that nginx version via `/usr/sbin/nginx -v`, downloading the
#      matching source tarball from https://nginx.org/download/, and extracting
#      it to a directory it then passes to this script as $1.
#
# This script does NOT perform version detection or downloads — it only runs
# configure + make against the already-extracted source tree.
#
# Usage:
#   build-module.sh <nginx-source-dir> <module-src> <dynamic|static> [extra-add-module-path ...]
#
#   nginx-source-dir  absolute path to the extracted nginx source tree (the dir
#                     containing the `configure` script). The Dockerfile sets
#                     this up via `tar -xzf nginx-X.Y.Z.tar.gz`.
#   module-src        absolute path to the zstd-nginx-module checkout (the dir
#                     containing the top-level `config` file).
#   build-mode        "dynamic" → ./configure --with-compat --add-dynamic-module=<module>; make modules
#                     "static"  → ./configure --add-module=<module>; make
#   extra paths       additional module paths appended as further
#                     --add-dynamic-module / --add-module entries (e.g. ngx_brotli
#                     for the brotli variant).
#
# Output artifacts:
#   dynamic → <nginx-source-dir>/objs/*.so
#   static  → <nginx-source-dir>/objs/nginx
# The Dockerfile COPY step picks whichever artifact it needs from there.

set -euxo pipefail

if [ "$#" -lt 3 ]; then
    echo "usage: $0 <nginx-source-dir> <module-src> <dynamic|static> [extra-add-module ...]" >&2
    exit 2
fi

NGINX_SRC_DIR="$1"
MODULE_SRC="$2"
MODE="$3"
shift 3

case "$MODE" in
    dynamic|static) ;;
    *) echo "error: build-mode must be 'dynamic' or 'static' (got '$MODE')" >&2; exit 2 ;;
esac

if [ ! -f "$NGINX_SRC_DIR/configure" ]; then
    echo "error: $NGINX_SRC_DIR/configure not found (is this an extracted nginx source tree?)" >&2
    exit 2
fi

if [ ! -f "$MODULE_SRC/config" ]; then
    echo "error: $MODULE_SRC/config not found (is this the module root?)" >&2
    exit 2
fi

cd "$NGINX_SRC_DIR"

# Assemble configure args. For dynamic builds --with-compat lets the resulting
# .so load under any distro-shipped nginx binary built with --with-compat
# (nginx.org packages are built this way). For static builds we link all
# requested modules into a fresh nginx binary.
#
# Module order matters: extras are added BEFORE the main module so that
# filter/config and static/config in the main module can sed-reorder
# themselves relative to extras (which by then are already in
# HTTP_FILTER_MODULES / HTTP_MODULES). Specifically: zstd's filter/config
# moves zstd_filter after brotli_filter, and static/config moves
# zstd_static after brotli_static — both only work if brotli's config
# has already populated those lists. Without this order, ngx_brotli's
# static module ends up later in ngx_modules[] than zstd_static and wins
# the content phase (nginx core reverses the handler array in
# ngx_http_init_phase_handlers, so "later" = "called first").
CONFIGURE_ARGS=()
if [ "$MODE" = "dynamic" ]; then
    CONFIGURE_ARGS+=(--with-compat)
    for extra in "$@"; do
        CONFIGURE_ARGS+=(--add-dynamic-module="$extra")
    done
    CONFIGURE_ARGS+=(--add-dynamic-module="$MODULE_SRC")
else
    # static build pulls in http_ssl + http_v2 so a realistic nginx is
    # produced; the gzip filter is on by default so filter/config's
    # filter-priority sed can exercise brotli > zstd > gzip ordering at
    # link time. http_v2 makes the brotli variant usable by pytest
    # test_h2_truncation / test_http2_proxy_flush (they skip otherwise).
    CONFIGURE_ARGS+=(--with-http_ssl_module --with-http_v2_module)
    for extra in "$@"; do
        CONFIGURE_ARGS+=(--add-module="$extra")
    done
    CONFIGURE_ARGS+=(--add-module="$MODULE_SRC")
fi

./configure "${CONFIGURE_ARGS[@]}"

if [ "$MODE" = "dynamic" ]; then
    make -j"$(nproc)" modules
else
    make -j"$(nproc)"
fi

# Report where artifacts live so the Dockerfile COPY step is obvious.
ls -la objs/*.so 2>/dev/null || true
ls -la objs/nginx 2>/dev/null || true
