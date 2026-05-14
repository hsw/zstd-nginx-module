#!/bin/bash
# accept-encoding.sh — regression for Task 5 (RFC 9110 Accept-Encoding parser).
#
# The original parser used naive ngx_strstrn() on the header value, which
# misclassified five real-world inputs:
#   1. `zstd;q=0`         — q=0 means NOT acceptable; original served zstd anyway.
#   2. `gzip, zstd`       — multiple tokens; original picked the first match
#                           regardless of position.
#   3. `zstdx`            — substring match: "zstd" appears as a prefix of an
#                           unrelated token; original served zstd.
#   4. `ZSTD`             — case sensitivity; original was case-sensitive in
#                           one branch.
#   5. `br;q=0.5, zstd;q=1` — q-value comparison; original ignored q entirely.
#
# Task 5 rewrote the parser to match nginx-core's gzip behaviour (per RFC 9110
# §12.5.3). This script asserts all five fixed cases.
#
# Runs inside the container.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_common.sh"

LABEL="accept-encoding"

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

# We use /text instead of / because the root response is "ok\n" — too short for
# zstd to bother compressing even with zstd_min_length 0 in some libzstd
# builds. /text has a 180-byte repetitive body that always compresses.
URL="http://127.0.0.1:8080/text"

case_zstd_present() {
    local label="$1"; local ae="$2"
    local headers
    headers="$(curl -sSI -H "Accept-Encoding: ${ae}" "$URL")" || {
        _log_fail "${LABEL}/${label}" "curl failed"
        FAIL=$((FAIL + 1)); return
    }
    if assert_header "$headers" "Content-Encoding" "zstd" 2>/dev/null; then
        _log_pass "${LABEL}/${label}"
        PASS=$((PASS + 1))
    else
        _log_fail "${LABEL}/${label}" "expected Content-Encoding: zstd"
        printf '%s\n' "$headers" >&2
        FAIL=$((FAIL + 1))
    fi
}

case_zstd_absent() {
    local label="$1"; local ae="$2"
    local headers
    headers="$(curl -sSI -H "Accept-Encoding: ${ae}" "$URL")" || {
        _log_fail "${LABEL}/${label}" "curl failed"
        FAIL=$((FAIL + 1)); return
    }
    if assert_header_absent "$headers" "Content-Encoding" 2>/dev/null; then
        _log_pass "${LABEL}/${label}"
        PASS=$((PASS + 1))
    else
        # Stricter form: header may exist but MUST NOT equal zstd.
        local ce
        ce="$(printf '%s\n' "$headers" | tr -d '\r' \
            | awk -F': ' 'tolower($1)=="content-encoding" {sub(/^[^:]*: */,""); print; exit}')"
        if [ "$ce" != "zstd" ]; then
            _log_pass "${LABEL}/${label} (Content-Encoding=${ce:-<none>})"
            PASS=$((PASS + 1))
        else
            _log_fail "${LABEL}/${label}" "Content-Encoding=zstd, must not be"
            FAIL=$((FAIL + 1))
        fi
    fi
}

case_zstd_absent "q=0"          "zstd;q=0"
case_zstd_present "multi-token" "gzip, zstd"
case_zstd_absent "false-prefix" "zstdx"
case_zstd_present "case-insens" "ZSTD"
case_zstd_present "q-wins"      "br;q=0.5, zstd;q=1"

echo "${LABEL}: pass=${PASS} fail=${FAIL}"
[ "$FAIL" -eq 0 ]
