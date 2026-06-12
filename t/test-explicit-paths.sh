#!/bin/bash
# t/test-explicit-paths.sh — self-test for the explicit-path branch of
# filter/config and static/config.
#
# Regression coverage for codex4 P1.2: when the operator passes
# ZSTD_INC=<dir> ZSTD_LIB=<dir> on a --add-dynamic-module build, the
# explicit-path branch must prefer the SHARED libzstd (-L… -lzstd …) and
# NOT try `$ZSTD_LIB/libzstd.a` first. Distro libzstd.a is almost never
# built with -fPIC, so linking the archive into a .so fails with
# R_X86_64_PC32 relocation errors. The old code path would silently
# emit a broken .so or fail with a confusing link error.
#
# This script re-runs nginx configure + `make modules` inside the
# already-built ubuntu-24.04 test image, with ZSTD_INC/ZSTD_LIB pointed
# at the distro layout. It asserts:
#   (1) configure succeeds and reports the shared-library feature line,
#   (2) `make modules` produces a .so,
#   (3) `nginx -t` with the .so loaded passes config validation.
#
# Skip if the test image isn't built (e.g. fresh checkout, no Docker).
# Exit codes follow the autoconf SKIP convention:
#   0  pass
#   77 skip (environment can't run this test — docker missing / image absent)
#   *  fail
# t/run.sh distinguishes 0 from 77 so a skipped run is NOT counted as pass.
#
# Coverage scope note: this self-test exercises ONLY the --add-dynamic-module
# branch of filter/config + static/config (the codex4 P1.2 fix that prefers
# shared libzstd over libzstd.a). The static --add-module / HTTP_FILTER_MODULES
# sed-rewrite path is preserved verbatim from upstream — no behavioural change
# — and is therefore intentionally NOT covered here. See filter/config and
# static/config for the static-archive code path; the `ubuntu-24.04-shared-only`
# matrix variant already exercises auto-discovery for static builds.

set -uo pipefail

IMAGE=${IMAGE:-zstd-nginx-test:ubuntu-24.04}

if ! command -v docker >/dev/null 2>&1; then
    echo "SKIP: docker not on PATH"
    exit 77
fi

# Use `docker images -q` (not `docker image inspect`) for the presence check:
# under Docker Desktop's containerd-snapshotter, `image inspect` by name:tag
# returns "No such image" for buildkit-produced multi-arch manifest entries
# even when the image is fully usable via `docker run`. `images -q` resolves
# the tag correctly in both stores. Mirrors the presence check in t/run.sh.
if [ -z "$(docker images -q "$IMAGE" 2>/dev/null)" ]; then
    echo "SKIP: image $IMAGE not built (run \`bash t/build.sh ubuntu-24.04\` first)"
    exit 77
fi

# Detect arch inside the image so the test works on amd64 hosts (linux/amd64
# emulated under Rosetta on arm64 macOS, or native amd64 CI runners) AND
# on arm64 hosts where the test image was built natively.
ZSTD_ARCH=$(docker run --rm "$IMAGE" sh -c 'dpkg-architecture -qDEB_HOST_MULTIARCH 2>/dev/null || gcc -print-multiarch 2>/dev/null || echo x86_64-linux-gnu')
ZSTD_LIB_DIR="/usr/lib/${ZSTD_ARCH}"

echo "Test arch: $ZSTD_ARCH"
echo "ZSTD_LIB=$ZSTD_LIB_DIR"

# Mount the CURRENT working tree at /src so any local edits to
# filter/config + static/config are picked up — the baked /src in
# the image is a snapshot from image-build time. The test must run
# against today's config files, not a stale image layer.
REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)

LOG=$(mktemp)
trap 'rm -f "$LOG"' EXIT

docker run --rm \
    -v "$REPO_ROOT:/src-current:ro" \
    -e ZSTD_INC=/usr/include \
    -e ZSTD_LIB="$ZSTD_LIB_DIR" \
    "$IMAGE" bash -c '
        set -euo pipefail
        # /usr/local/src/nginx is a SYMLINK to the versioned tree whose objs/
        # already holds image-build-time .so artifacts; cp -aL dereferences
        # into a real copy and rm -rf objs guarantees the assertions below
        # can only pass against artifacts built HERE, not stale baked ones.
        cp -aL /usr/local/src/nginx /tmp/nginx-build
        rm -rf /tmp/nginx-build/objs
        cd /tmp/nginx-build
        # Reuse the same flags the image build used: --with-compat
        # --add-dynamic-module=<repo>. /src-current is the live host
        # checkout mounted read-only at run time.
        ./configure --with-compat --add-dynamic-module=/src-current 2>&1
        echo "=== configure-done ==="
        # Assert the explicit-path branch picked the shared lib by
        # grepping the feature probe output.
        grep -E "ZStandard dynamic library in /usr/include and /usr/lib/" objs/autoconf.err >/dev/null \
            || { echo "FAIL: expected dynamic-library feature line in autoconf.err"; cat objs/autoconf.err | tail -40; exit 1; }
        # Build modules.
        make -j"$(nproc)" modules 2>&1 | tail -5
        ls -la objs/*.so
        # Verify the .so loads under the system nginx via -t.
        mkdir -p /etc/nginx/modules-test
        cp objs/ngx_http_zstd_filter_module.so /etc/nginx/modules-test/
        cp objs/ngx_http_zstd_static_module.so /etc/nginx/modules-test/
        cat > /tmp/test-nginx.conf <<EOF
load_module /etc/nginx/modules-test/ngx_http_zstd_filter_module.so;
load_module /etc/nginx/modules-test/ngx_http_zstd_static_module.so;
events {}
http {
    server {
        listen 18080;
        location / { return 200 "ok"; }
    }
}
EOF
        nginx -c /tmp/test-nginx.conf -t
    ' 2>&1 | tee "$LOG"

rc=${PIPESTATUS[0]}

if [ "$rc" -ne 0 ]; then
    echo "FAIL: explicit-path dynamic build failed (exit $rc)"
    exit 1
fi

if ! grep -q "configuration file .* test is successful" "$LOG"; then
    echo "FAIL: nginx -t did not report success"
    exit 1
fi

echo "PASS: explicit-path dynamic build links shared libzstd and loads cleanly"
