#!/bin/bash
# h2-truncation.sh — regression for PR #49 / Task 3.
#
# Background: ngx_http_zstd_filter_module flushes its output buffer when the
# accumulated compressed size hits 128 KiB. The original code lost the trailing
# bytes when the next call to ZSTD_compressStream2 produced more output without
# the filter signalling NGX_AGAIN. Over HTTP/1.1 the framing usually papered
# over this (chunked encoding still terminated cleanly); HTTP/2 propagated the
# truncation as a short DATA frame and the decompressor failed.
#
# Coverage: serve deterministic bodies of 200000, 131071, 131072, 131073 bytes
# over HTTP/2, decompress via `zstd -d`, byte-diff against the original. All
# four sizes must round-trip byte-identical.
#
# Runs inside the container (docker exec). nginx, curl, zstd CLI all present.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_common.sh"

LABEL="h2-truncation"
TMPDIR="$(mktemp -d)"
trap 'cleanup' EXIT

cleanup() {
    stop_local_nginx
    rm -rf "$TMPDIR"
}

render_conf() {
    # HTTP/2 cleartext (h2c) requires `http2 on;` at http or server scope.
    # nginx.org mainline (1.25.1+) accepts the directive at http context.
    sed \
        -e 's|__LOAD_MODULES__|load_module modules/ngx_http_zstd_filter_module.so;|' \
        -e 's|__EXTRA_DIRECTIVES__|http2 on;|' \
        -e 's|__EXTRA_LOCATIONS__||' \
        -e 's|__SERVER_PORT__|8080|' \
        /etc/nginx/templates/nginx.conf.template > /etc/nginx/nginx.conf

    # If the binary was statically linked (no load_module support), strip the
    # load_module line that the template still emits.
    if ! nginx -V 2>&1 | grep -q -- '--with-compat'; then
        sed -i '/^load_module /d' /etc/nginx/nginx.conf
    fi
    apply_daemon_mode /etc/nginx/nginx.conf
}

# Skip the script gracefully if the nginx in this image wasn't built with
# http_v2 (the brotli variant's static configure line omits it). Test
# applicability is preserved without making the driver hard-code another
# variant carve-out.
if ! nginx -V 2>&1 | grep -q -- '--with-http_v2_module'; then
    echo "${LABEL}: skipping — nginx not built with --with-http_v2_module" >&2
    exit 0
fi

mkdir -p /var/fixtures/random
render_conf
start_local_nginx_bg /etc/nginx/nginx.conf

PASS=0
FAIL=0

run_size() {
    local n="$1"
    local src="/var/fixtures/random/${n}"
    local out="${TMPDIR}/${n}.body"
    local dec="${TMPDIR}/${n}.dec"

    # Generate deterministic body once per size. /dev/urandom is fine — we
    # never compare across runs, only within a single curl-then-zstd round-trip.
    if [ ! -f "$src" ]; then
        head -c "$n" /dev/urandom > "$src"
    fi

    # --http2-prior-knowledge: speak h2c without an upgrade dance. The original
    # PR #49 reproduction relied on streaming many DATA frames; the prior-
    # knowledge path is sufficient and works against `http2 on;` cleartext.
    curl -sS --http2-prior-knowledge \
        -H "Accept-Encoding: zstd" \
        --max-time 30 \
        "http://127.0.0.1:8080/random/${n}" -o "$out" || {
        _log_fail "${LABEL}/${n}" "curl failed"
        FAIL=$((FAIL + 1))
        return
    }

    if ! zstd -dc -- "$out" > "$dec" 2>/tmp/zstd-err; then
        _log_fail "${LABEL}/${n}" "zstd -d failed: $(cat /tmp/zstd-err)"
        FAIL=$((FAIL + 1))
        return
    fi

    if ! cmp -s "$src" "$dec"; then
        _log_fail "${LABEL}/${n}" \
            "decoded body differs from origin (orig=$(wc -c < "$src") dec=$(wc -c < "$dec"))"
        FAIL=$((FAIL + 1))
        return
    fi

    _log_pass "${LABEL}/${n}"
    PASS=$((PASS + 1))
}

# Four sizes around the 128 KiB flush boundary that PR #49 fixed. 131072 is the
# exact boundary where the original off-by-one truncated trailing bytes.
for n in 200000 131071 131072 131073; do
    run_size "$n"
done

echo "${LABEL}: pass=${PASS} fail=${FAIL}"
[ "$FAIL" -eq 0 ]
