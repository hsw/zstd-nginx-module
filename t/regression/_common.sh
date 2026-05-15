#!/bin/bash
# _common.sh — shared helpers sourced by every regression script in t/regression/.
# All helpers exit nonzero on failure so the calling script can `set -e`.
#
# Conventions:
#   * Regression scripts render an nginx config and run nginx in-container via
#     start_local_nginx / stop_local_nginx — no separate docker container is
#     started by these helpers.
#   * Default test port is 8080; override with $ZSTD_TEST_PORT.

set -uo pipefail

ZSTD_TEST_PORT="${ZSTD_TEST_PORT:-8080}"

# assert_header <headers-blob> <header-name> <expected-value>
# Case-insensitive header name match; exact value match. Exits 1 on failure.
assert_header() {
    local blob="$1"
    local name="$2"
    local expected="$3"
    local actual
    actual="$(printf '%s\n' "$blob" \
        | tr -d '\r' \
        | awk -v IGNORECASE=1 -v h="$name" -F': ' \
              'tolower($1) == tolower(h) { sub(/^[^:]*: */, ""); print; exit }')"
    if [ "$actual" != "$expected" ]; then
        echo "assert_header: ${name}: expected [${expected}] got [${actual}]" >&2
        echo "--- full headers ---" >&2
        printf '%s\n' "$blob" >&2
        return 1
    fi
}

# assert_header_absent <headers-blob> <header-name>
# Fails if the named header appears at all.
assert_header_absent() {
    local blob="$1"
    local name="$2"
    if printf '%s\n' "$blob" | tr -d '\r' | awk -v IGNORECASE=1 -v h="$name" -F': ' \
            'tolower($1) == tolower(h) { found=1 } END { exit !found }'; then
        echo "assert_header_absent: ${name} present but should be absent" >&2
        printf '%s\n' "$blob" >&2
        return 1
    fi
}

# stop_local_nginx — graceful then forceful teardown of an in-container nginx.
# Tries `nginx -s stop` (matches the config the test rendered), waits up to ~2s
# for the master to release /tmp/nginx.pid, then escalates to SIGTERM via
# pkill, then SIGKILL. Idempotent. Designed for in-container regression scripts
# that re-render config and bring nginx up multiple times.
stop_local_nginx() {
    local conf="${1:-/etc/nginx/nginx.conf}"
    nginx -c "$conf" -s stop >/dev/null 2>&1 || true
    local i=0
    while [ -f /tmp/nginx.pid ] && [ "$i" -lt 20 ]; do
        sleep 0.1
        i=$((i + 1))
    done
    # Forceful escalation if graceful stop didn't take effect (e.g. the
    # invoked binary was launched with a different `-c` than we asked here).
    if [ -f /tmp/nginx.pid ] || pgrep -x nginx >/dev/null 2>&1; then
        pkill -TERM -x nginx >/dev/null 2>&1 || true
        i=0
        while pgrep -x nginx >/dev/null 2>&1 && [ "$i" -lt 30 ]; do
            sleep 0.1
            i=$((i + 1))
        done
        pkill -KILL -x nginx >/dev/null 2>&1 || true
    fi
    rm -f /tmp/nginx.pid
}

# _log_pass <label> — pretty-prints a green-ish PASS line so regression scripts
# share a uniform output format. No exit semantics; pure logging.
_log_pass() {
    local label="${1:-unnamed}"
    printf '  pass  %s\n' "$label"
}

# _log_fail <label> [extra-detail ...] — pretty-prints a FAIL line. Extra args
# are echoed verbatim on the next indented lines for context. No exit semantics
# — caller decides whether failures abort or accumulate.
_log_fail() {
    local label="${1:-unnamed}"
    shift || true
    printf '  fail  %s\n' "$label" >&2
    if [ "$#" -gt 0 ]; then
        printf '        %s\n' "$@" >&2
    fi
}
