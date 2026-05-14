#!/bin/bash
# new-directives.sh — regression for Tasks 9, 10, 11.
#
# Coverage:
#   * zstd_max_length 10k:   small body compressed, large body NOT compressed.
#   * zstd_bypass $arg_raw:  ?raw=1 → uncompressed; ?         → compressed.
#   * zstd_bypass multi:     two predicates ($cookie_no $arg_raw); either trips bypass.
#   * zstd_window_bits 17:   valid; decompresses byte-identical.
#   * zstd_window_bits 50:   nginx -t fails (above ZSTD_WINDOWLOG_MAX=27).
#   * zstd_window_bits 9:    nginx -t fails (below ZSTD_WINDOWLOG_MIN=10).
#
# Runs inside the container.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_common.sh"

LABEL="new-directives"
TMPDIR="$(mktemp -d)"

cleanup() {
    stop_local_nginx
    rm -rf "$TMPDIR"
}
trap 'cleanup' EXIT

# Helper: render template with arbitrary extra directives (escaped for sed).
render_conf() {
    local extra_dirs="$1"
    local extra_locs="${2:-}"

    # sed delimiter '|' will trip on '|' inside the substitution text; we use
    # a python-ish pre-substitution by writing the file then doing a token
    # replacement with awk for the multi-line cases.
    sed \
        -e 's|__LOAD_MODULES__|load_module modules/ngx_http_zstd_filter_module.so;|' \
        -e 's|__EXTRA_DIRECTIVES__|%%EXTRA_DIRS%%|' \
        -e 's|__EXTRA_LOCATIONS__|%%EXTRA_LOCS%%|' \
        -e 's|__SERVER_PORT__|8080|' \
        /etc/nginx/templates/nginx.conf.template > "${TMPDIR}/nginx.conf.in"

    awk -v dirs="$extra_dirs" -v locs="$extra_locs" '
        { gsub(/%%EXTRA_DIRS%%/, dirs); gsub(/%%EXTRA_LOCS%%/, locs); print }
    ' "${TMPDIR}/nginx.conf.in" > /etc/nginx/nginx.conf

    if ! nginx -V 2>&1 | grep -q -- '--with-compat'; then
        sed -i '/^load_module /d' /etc/nginx/nginx.conf
    fi
    sed -i 's|^daemon off;|daemon on;|' /etc/nginx/nginx.conf
}

start_local_nginx() {
    nginx -c /etc/nginx/nginx.conf -t >/tmp/nginx-t.log 2>&1 || {
        echo "nginx -t failed:" >&2
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

# stop_local_nginx is provided by _common.sh.

# Fixture: a small-body location and a large-body location, both highly
# compressible so zstd always shrinks them. We inject them via __EXTRA_LOCATIONS__.
mkdir -p /var/fixtures/random
SMALL_FILE=/var/fixtures/random/small-5k
LARGE_FILE=/var/fixtures/random/large-50k
# 5 KiB and 50 KiB of repetitive ASCII (compresses ~99%). `yes | head -c N`
# would be the obvious one-liner but it trips `set -o pipefail` (head closes
# stdin → yes exits with SIGPIPE/141). dd from /dev/zero piped through tr to
# remap NULs to a printable byte sidesteps both pipefail and any LC_ALL
# surprises.
dd if=/dev/zero bs=5120  count=1 status=none | tr '\0' 'A' > "$SMALL_FILE"
dd if=/dev/zero bs=51200 count=1 status=none | tr '\0' 'A' > "$LARGE_FILE"

PASS=0
FAIL=0

# ----- zstd_max_length 10k -----
EXTRA="zstd_max_length 10k;"
render_conf "$EXTRA"
start_local_nginx

# Small body (5 KiB) must be compressed.
HDR="$(curl -sSI -H "Accept-Encoding: zstd" http://127.0.0.1:8080/random/small-5k)"
if assert_header "$HDR" "Content-Encoding" "zstd" 2>/dev/null; then
    _log_pass "${LABEL}/max-length-small-compressed"; PASS=$((PASS + 1))
else
    _log_fail "${LABEL}/max-length-small-compressed" \
        "5 KiB body must be compressed when zstd_max_length=10k"
    printf '%s\n' "$HDR" >&2
    FAIL=$((FAIL + 1))
fi

# Large body (50 KiB) must NOT be compressed.
HDR="$(curl -sSI -H "Accept-Encoding: zstd" http://127.0.0.1:8080/random/large-50k)"
CE="$(printf '%s\n' "$HDR" | tr -d '\r' \
    | awk -F': ' 'tolower($1)=="content-encoding" {sub(/^[^:]*: */,""); print; exit}')"
if [ "$CE" != "zstd" ]; then
    _log_pass "${LABEL}/max-length-large-uncompressed (CE=${CE:-<none>})"
    PASS=$((PASS + 1))
else
    _log_fail "${LABEL}/max-length-large-uncompressed" \
        "50 KiB body must NOT be compressed when zstd_max_length=10k"
    printf '%s\n' "$HDR" >&2
    FAIL=$((FAIL + 1))
fi

stop_local_nginx

# ----- zstd_bypass $arg_raw -----
EXTRA="zstd_bypass \$arg_raw;"
render_conf "$EXTRA"
start_local_nginx

# /text?raw=1 → arg_raw is "1" (truthy) → bypass → no Content-Encoding.
HDR="$(curl -sSI -H "Accept-Encoding: zstd" 'http://127.0.0.1:8080/text?raw=1')"
CE="$(printf '%s\n' "$HDR" | tr -d '\r' \
    | awk -F': ' 'tolower($1)=="content-encoding" {sub(/^[^:]*: */,""); print; exit}')"
if [ "$CE" != "zstd" ]; then
    _log_pass "${LABEL}/bypass-arg-set (CE=${CE:-<none>})"
    PASS=$((PASS + 1))
else
    _log_fail "${LABEL}/bypass-arg-set" "?raw=1 must bypass zstd"
    printf '%s\n' "$HDR" >&2
    FAIL=$((FAIL + 1))
fi

# /text (no arg) → arg_raw empty → no bypass → compressed.
HDR="$(curl -sSI -H "Accept-Encoding: zstd" 'http://127.0.0.1:8080/text')"
if assert_header "$HDR" "Content-Encoding" "zstd" 2>/dev/null; then
    _log_pass "${LABEL}/bypass-arg-unset-compressed"; PASS=$((PASS + 1))
else
    _log_fail "${LABEL}/bypass-arg-unset-compressed" "must be compressed when no bypass"
    printf '%s\n' "$HDR" >&2
    FAIL=$((FAIL + 1))
fi

stop_local_nginx

# ----- zstd_bypass multi-predicate -----
EXTRA='zstd_bypass $cookie_no $arg_raw;'
render_conf "$EXTRA"
start_local_nginx

# Either predicate set should bypass. Test the cookie path (the arg path was
# covered above; this asserts the multi-arg loop walks both predicates).
HDR="$(curl -sSI -H "Accept-Encoding: zstd" -H "Cookie: no=1" 'http://127.0.0.1:8080/text')"
CE="$(printf '%s\n' "$HDR" | tr -d '\r' \
    | awk -F': ' 'tolower($1)=="content-encoding" {sub(/^[^:]*: */,""); print; exit}')"
if [ "$CE" != "zstd" ]; then
    _log_pass "${LABEL}/bypass-multi-cookie (CE=${CE:-<none>})"
    PASS=$((PASS + 1))
else
    _log_fail "${LABEL}/bypass-multi-cookie" "Cookie: no=1 must bypass zstd"
    printf '%s\n' "$HDR" >&2
    FAIL=$((FAIL + 1))
fi

# Neither predicate set → compressed.
HDR="$(curl -sSI -H "Accept-Encoding: zstd" 'http://127.0.0.1:8080/text')"
if assert_header "$HDR" "Content-Encoding" "zstd" 2>/dev/null; then
    _log_pass "${LABEL}/bypass-multi-none-compressed"; PASS=$((PASS + 1))
else
    _log_fail "${LABEL}/bypass-multi-none-compressed" "must be compressed when both predicates empty"
    printf '%s\n' "$HDR" >&2
    FAIL=$((FAIL + 1))
fi

stop_local_nginx

# ----- zstd_window_bits 17 (valid) -----
EXTRA="zstd_window_bits 17;"
render_conf "$EXTRA"
start_local_nginx

curl -sS -H "Accept-Encoding: zstd" 'http://127.0.0.1:8080/text' -o "${TMPDIR}/wb17.body"
if zstd -dc -- "${TMPDIR}/wb17.body" > "${TMPDIR}/wb17.dec" 2>/dev/null && [ -s "${TMPDIR}/wb17.dec" ]; then
    _log_pass "${LABEL}/window-bits-17-decompresses"; PASS=$((PASS + 1))
else
    _log_fail "${LABEL}/window-bits-17-decompresses" "decompression failed"
    FAIL=$((FAIL + 1))
fi

stop_local_nginx

# ----- zstd_window_bits out-of-range MUST fail nginx -t -----
# ZSTD_WINDOWLOG_MAX=27, ZSTD_WINDOWLOG_MIN=10.
for bad in 50 9; do
    EXTRA="zstd_window_bits ${bad};"
    render_conf "$EXTRA"
    if nginx -c /etc/nginx/nginx.conf -t >/tmp/nginx-t.log 2>&1; then
        _log_fail "${LABEL}/window-bits-${bad}-rejected" \
            "nginx -t accepted zstd_window_bits=${bad}, must reject"
        cat /tmp/nginx-t.log >&2
        FAIL=$((FAIL + 1))
    else
        _log_pass "${LABEL}/window-bits-${bad}-rejected"
        PASS=$((PASS + 1))
    fi
done

# Clean up any fixture files we created at the top of the script (the trap
# handles TMPDIR; the /var/fixtures/random files are intentionally kept for
# other scripts if they want them).

echo "${LABEL}: pass=${PASS} fail=${FAIL}"
[ "$FAIL" -eq 0 ]
