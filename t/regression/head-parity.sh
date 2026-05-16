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
TMPDIR="$(mktemp -d)"

cleanup() {
    stop_local_nginx
    rm -rf "$TMPDIR"
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
    apply_daemon_mode /etc/nginx/nginx.conf
}

render_conf
start_local_nginx_bg /etc/nginx/nginx.conf

PASS=0
FAIL=0

URL="http://127.0.0.1:8080/text"

# 1. HEAD request: must include Content-Encoding: zstd, Vary: Accept-Encoding,
#    and MUST NOT include Content-Length (the encoded length is unknown at
#    header time when compression streams). nginx switches to chunked
#    (HTTP/1.1) instead.
#    `-X HEAD` (not `-I`) so curl writes only the body bytes to -o; with `-I`
#    curl mirrors response headers into the body stream, which would defeat
#    the HEAD body-empty assertion below.
curl -sS -X HEAD -D "${TMPDIR}/head.hdr" -o "${TMPDIR}/head.body" \
    -H "Accept-Encoding: zstd" "$URL"
HEAD_HEADERS="$(cat "${TMPDIR}/head.hdr")"

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

# HEAD response MUST NOT have a body — RFC 9110 §9.3.2 forbids it regardless
# of the response framing. With chunked transfer-encoding, an over-zealous
# body filter could emit a single empty terminating chunk (5 bytes: "0\r\n\r\n").
# curl writes the body bytes to -o, so a non-zero file size signals a bug.
HEAD_BODY_SIZE="$(wc -c < "${TMPDIR}/head.body" | tr -d ' ')"
if [ "$HEAD_BODY_SIZE" -eq 0 ]; then
    _log_pass "${LABEL}/head-empty-body"; PASS=$((PASS + 1))
else
    _log_fail "${LABEL}/head-empty-body" "HEAD body is ${HEAD_BODY_SIZE} bytes, expected 0"
    FAIL=$((FAIL + 1))
fi

# 2. GET request: same three header invariants plus the body must decompress.

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

# 3. ETag parity. Synthetic `return 200 "..."` locations don't emit ETag, so
#    use a file-served fixture (nginx core stamps weak ETag from file mtime+size).
#    HEAD and GET must both carry the same ETag value — zstd's header filter
#    weakens strong ETags by prepending W/, applied identically on HEAD and GET.
mkdir -p /var/fixtures/random
ETAG_FIXTURE=/var/fixtures/random/etag-fixture
if [ ! -s "$ETAG_FIXTURE" ]; then
    dd if=/dev/zero bs=2048 count=1 status=none | tr '\0' 'Z' > "$ETAG_FIXTURE"
fi

ETAG_URL="http://127.0.0.1:8080/random/etag-fixture"
curl -sS -I -D "${TMPDIR}/etag-head.hdr" -o /dev/null \
    -H "Accept-Encoding: zstd" "$ETAG_URL"
curl -sS -D "${TMPDIR}/etag-get.hdr" -o "${TMPDIR}/etag-get.body" \
    -H "Accept-Encoding: zstd" "$ETAG_URL"

extract_etag() {
    tr -d '\r' < "$1" \
        | awk -F': ' 'tolower($1)=="etag" {sub(/^[^:]*: */,""); print; exit}'
}
HEAD_ETAG="$(extract_etag "${TMPDIR}/etag-head.hdr")"
GET_ETAG="$(extract_etag "${TMPDIR}/etag-get.hdr")"

if [ -n "$HEAD_ETAG" ] && [ -n "$GET_ETAG" ] && [ "$HEAD_ETAG" = "$GET_ETAG" ]; then
    _log_pass "${LABEL}/etag-match (${HEAD_ETAG})"; PASS=$((PASS + 1))
else
    _log_fail "${LABEL}/etag-match" \
        "HEAD ETag=[${HEAD_ETAG}] GET ETag=[${GET_ETAG}] (must be equal and non-empty)"
    FAIL=$((FAIL + 1))
fi

echo "${LABEL}: pass=${PASS} fail=${FAIL}"
[ "$FAIL" -eq 0 ]
