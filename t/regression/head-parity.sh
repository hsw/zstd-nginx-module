#!/bin/bash
# head-parity.sh — regression for Task 6 (HEAD parity).
#
# Background: nginx core short-circuits HEAD by skipping the response body
# phase, which used to fire BEFORE the zstd header filter ran. The result:
# HEAD /foo returned no Content-Encoding header while GET /foo did. Browsers
# and CDNs that probe with HEAD then ship an unencoded GET would skip zstd
# entirely.
#
# Fix: run the header-stamping logic before the body short-circuit so HEAD and
# GET return identical headers (Content-Encoding: zstd, Vary: Accept-Encoding,
# no Content-Length on the compressed transfer).
#
# Runs inside the container.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_common.sh"

LABEL="head-parity"

cleanup() {
    stop_local_nginx
}
trap 'cleanup' EXIT

render_conf() {
    sed \
        -e 's|__LOAD_MODULES__|load_module modules/ngx_http_zstd_filter_module.so;|' \
        -e 's|__EXTRA_DIRECTIVES__||' \
        -e 's|__EXTRA_LOCATIONS__||' \
        -e 's|__SERVER_PORT__|8080|' \
        /etc/nginx/templates/nginx.conf.template > /etc/nginx/nginx.conf

    if ! nginx -V 2>&1 | grep -q -- '--with-compat'; then
        sed -i '/^load_module /d' /etc/nginx/nginx.conf
    fi
    sed -i 's|^daemon off;|daemon on;|' /etc/nginx/nginx.conf
}

start_local_nginx() {
    nginx -c /etc/nginx/nginx.conf -t >/tmp/nginx-t.log 2>&1 || {
        echo "nginx -t failed" >&2
        cat /tmp/nginx-t.log >&2
        return 1
    }
    nginx -c /etc/nginx/nginx.conf
    local i=0
    while ! curl -fsS --max-time 1 http://127.0.0.1:8080/ >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -ge 30 ]; then
            echo "nginx did not start within 3s" >&2
            return 1
        fi
        sleep 0.1
    done
}

render_conf
start_local_nginx

PASS=0
FAIL=0

URL="http://127.0.0.1:8080/text"

# 1. HEAD request: must include Content-Encoding: zstd, Vary: Accept-Encoding,
#    and MUST NOT include Content-Length (the encoded length is unknown at
#    header time when compression streams). nginx switches to chunked
#    (HTTP/1.1) instead.
HEAD_HEADERS="$(curl -sSI -H "Accept-Encoding: zstd" "$URL")"

if assert_header "$HEAD_HEADERS" "Content-Encoding" "zstd" 2>/dev/null; then
    _log_pass "${LABEL}/head-content-encoding"; PASS=$((PASS + 1))
else
    _log_fail "${LABEL}/head-content-encoding" "expected zstd"
    printf '%s\n' "$HEAD_HEADERS" >&2
    FAIL=$((FAIL + 1))
fi

if assert_header "$HEAD_HEADERS" "Vary" "Accept-Encoding" 2>/dev/null; then
    _log_pass "${LABEL}/head-vary"; PASS=$((PASS + 1))
else
    _log_fail "${LABEL}/head-vary" "expected Vary: Accept-Encoding"
    printf '%s\n' "$HEAD_HEADERS" >&2
    FAIL=$((FAIL + 1))
fi

if assert_header_absent "$HEAD_HEADERS" "Content-Length" 2>/dev/null; then
    _log_pass "${LABEL}/head-no-content-length"; PASS=$((PASS + 1))
else
    _log_fail "${LABEL}/head-no-content-length" "Content-Length must be absent on zstd HEAD"
    printf '%s\n' "$HEAD_HEADERS" >&2
    FAIL=$((FAIL + 1))
fi

# 2. GET request: same three header invariants plus the body must decompress.
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"; cleanup' EXIT

curl -sS -D "${TMPDIR}/get.hdr" -o "${TMPDIR}/get.body" \
    -H "Accept-Encoding: zstd" "$URL"

GET_HEADERS="$(cat "${TMPDIR}/get.hdr")"

if assert_header "$GET_HEADERS" "Content-Encoding" "zstd" 2>/dev/null; then
    _log_pass "${LABEL}/get-content-encoding"; PASS=$((PASS + 1))
else
    _log_fail "${LABEL}/get-content-encoding" "expected zstd"; FAIL=$((FAIL + 1))
fi

if assert_header "$GET_HEADERS" "Vary" "Accept-Encoding" 2>/dev/null; then
    _log_pass "${LABEL}/get-vary"; PASS=$((PASS + 1))
else
    _log_fail "${LABEL}/get-vary" "expected Vary: Accept-Encoding"; FAIL=$((FAIL + 1))
fi

if assert_header_absent "$GET_HEADERS" "Content-Length" 2>/dev/null; then
    _log_pass "${LABEL}/get-no-content-length"; PASS=$((PASS + 1))
else
    _log_fail "${LABEL}/get-no-content-length" "Content-Length must be absent"
    FAIL=$((FAIL + 1))
fi

# Body must round-trip through zstd -d.
if zstd -dc -- "${TMPDIR}/get.body" > "${TMPDIR}/get.dec" 2>/dev/null; then
    if [ -s "${TMPDIR}/get.dec" ]; then
        _log_pass "${LABEL}/get-decompresses (size=$(wc -c < "${TMPDIR}/get.dec"))"
        PASS=$((PASS + 1))
    else
        _log_fail "${LABEL}/get-decompresses" "decoded body is empty"
        FAIL=$((FAIL + 1))
    fi
else
    _log_fail "${LABEL}/get-decompresses" "zstd -d failed"
    FAIL=$((FAIL + 1))
fi

echo "${LABEL}: pass=${PASS} fail=${FAIL}"
[ "$FAIL" -eq 0 ]
