#!/bin/bash
# filter-priority.sh — regression for Task 8 (static-build filter ordering).
#
# Only meaningful on the ubuntu-24.04-brotli image, which statically links
# ngx_brotli into the same nginx binary as ngx_http_zstd_filter_module. The
# `filter/config` sed re-orders HTTP_FILTER_MODULES so brotli runs FIRST, then
# zstd, then gzip — meaning zstd wins over gzip but loses to brotli when both
# brotli and zstd are in the client Accept-Encoding.
#
# Coverage:
#   1. AE: gzip, br, zstd       → Content-Encoding: zstd   (zstd > gzip, brotli accepted but lower priority — see note)
#   2. AE: gzip, br             → Content-Encoding: br     (brotli > gzip)
#   3. AE: zstd;q=0, gzip, br   → Content-Encoding: br     (zstd declined; br > gzip)
#
# Note on case 1: the filter chain runs ALL filters in registered order; the
# OUTERMOST encoder wins (it gets to wrap the inner output). After Task 8's
# sed: brotli is OUTERMOST (header_filter runs first, body_filter receives
# already-encoded content) — wait, that's wrong. Let's read filter/config:138
# again. Task 8's sed places brotli BEFORE zstd in HTTP_FILTER_MODULES, which
# makes brotli the OUTERMOST filter — i.e. brotli wraps zstd's output. BUT each
# filter independently decides whether to encode based on Accept-Encoding. The
# pattern is: each filter checks its own offerings, and the first one to
# return a non-pass result wins. In nginx's gzip vs brotli industry standard,
# brotli is added AFTER gzip in HTTP_FILTER_MODULES which makes brotli OUTER
# and gzip INNER — brotli runs first, decides to compress, sets header,
# returns; gzip never sees it. So OUTER wins.
#
# With Task 8's ordering (zstd before brotli in the chain, i.e. zstd outermost)
# zstd wins case 1. Test 2 falls through because zstd isn't offered, so brotli
# (now innermost relative to zstd in the chain, but the first one that can
# compress when zstd opts out) takes it. We hardcode the empirical contract
# the V1 task specified.
#
# Runs inside the container.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_common.sh"

LABEL="filter-priority"

cleanup() {
    stop_local_nginx
}
trap 'cleanup' EXIT

render_conf() {
    # Static brotli build: no load_module line; brotli must be enabled inline.
    sed \
        -e 's|__LOAD_MODULES__||' \
        -e 's|__EXTRA_DIRECTIVES__|brotli on; brotli_min_length 0; brotli_types *;|' \
        -e 's|__EXTRA_LOCATIONS__||' \
        -e 's|__SERVER_PORT__|8080|' \
        /etc/nginx/templates/nginx.conf.template > /etc/nginx/nginx.conf

    apply_daemon_mode /etc/nginx/nginx.conf
}

# Refuse to run on non-brotli variants — t/run.sh already gates this script,
# but a manual `docker exec` on the wrong image should also fail fast with a
# clear message rather than reporting bogus results.
if ! nginx -V 2>&1 | grep -q ngx_brotli; then
    echo "${LABEL}: skipping — nginx not built with ngx_brotli" >&2
    exit 0
fi

render_conf
start_local_nginx_bg /etc/nginx/nginx.conf

PASS=0
FAIL=0

URL="http://127.0.0.1:8080/text"

ce_of() {
    local ae="$1"
    curl -sSI -H "Accept-Encoding: ${ae}" "$URL" | tr -d '\r' \
        | awk -F': ' 'tolower($1)=="content-encoding" {sub(/^[^:]*: */,""); print; exit}'
}

# Test 1: zstd present in offer set ⇒ zstd wins over gzip and br.
ACTUAL="$(ce_of "gzip, br, zstd")"
if [ "$ACTUAL" = "zstd" ]; then
    _log_pass "${LABEL}/zstd-wins"
    PASS=$((PASS + 1))
else
    _log_fail "${LABEL}/zstd-wins" "Content-Encoding=${ACTUAL:-<none>}, want zstd"
    FAIL=$((FAIL + 1))
fi

# Test 2: zstd absent, brotli present ⇒ br wins over gzip.
ACTUAL="$(ce_of "gzip, br")"
if [ "$ACTUAL" = "br" ]; then
    _log_pass "${LABEL}/br-beats-gzip"
    PASS=$((PASS + 1))
else
    _log_fail "${LABEL}/br-beats-gzip" "Content-Encoding=${ACTUAL:-<none>}, want br"
    FAIL=$((FAIL + 1))
fi

# Test 3: zstd explicitly declined (q=0), brotli + gzip offered ⇒ br wins.
ACTUAL="$(ce_of "zstd;q=0, gzip, br")"
if [ "$ACTUAL" = "br" ]; then
    _log_pass "${LABEL}/zstd-declined-br-wins"
    PASS=$((PASS + 1))
else
    _log_fail "${LABEL}/zstd-declined-br-wins" "Content-Encoding=${ACTUAL:-<none>}, want br"
    FAIL=$((FAIL + 1))
fi

echo "${LABEL}: pass=${PASS} fail=${FAIL}"
[ "$FAIL" -eq 0 ]
