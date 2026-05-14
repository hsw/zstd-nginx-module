#!/bin/bash
# _common.sh — shared helpers sourced by every regression script in t/regression/.
# All helpers exit nonzero on failure so the calling script can `set -e`.
#
# Conventions:
#   * One nginx container per script; the helper tracks the container id in
#     $ZSTD_TEST_CID and cleans it up via trap-friendly stop_nginx.
#   * All `docker run` calls use --platform linux/amd64; developers on arm64
#     macOS rely on Rosetta/qemu emulation and the canonical build target is
#     amd64.
#   * Default test port is 8080; override with $ZSTD_TEST_PORT.

set -uo pipefail

ZSTD_TEST_PORT="${ZSTD_TEST_PORT:-8080}"
ZSTD_TEST_PLATFORM="${ZSTD_TEST_PLATFORM:-linux/amd64}"
ZSTD_TEST_CID=""

# start_nginx <image> [container-name-suffix]
# Boots the named image in the background, waits for it to listen on the test
# port, and stashes the container id in $ZSTD_TEST_CID.
start_nginx() {
    local image="$1"
    local suffix="${2:-default}"
    local name="zstd-test-${suffix}-$$"

    # Defensive cleanup of any orphan from a prior aborted run.
    docker rm -f "$name" >/dev/null 2>&1 || true

    ZSTD_TEST_CID="$(docker run -d --rm \
        --platform "$ZSTD_TEST_PLATFORM" \
        --name "$name" \
        -p "${ZSTD_TEST_PORT}:8080" \
        "$image")"

    if ! wait_listening "$ZSTD_TEST_PORT" 30; then
        echo "start_nginx: container did not listen on $ZSTD_TEST_PORT within 30s" >&2
        docker logs "$ZSTD_TEST_CID" >&2 || true
        stop_nginx
        return 1
    fi
}

# stop_nginx — best-effort teardown of the container started by start_nginx.
stop_nginx() {
    if [ -n "$ZSTD_TEST_CID" ]; then
        docker stop "$ZSTD_TEST_CID" >/dev/null 2>&1 || true
        ZSTD_TEST_CID=""
    fi
}

# wait_listening <port> <timeout-seconds>
# Polls GET /; returns 0 on first 2xx, 1 on timeout.
wait_listening() {
    local port="$1"
    local timeout="${2:-30}"
    local start now
    start="$(date +%s)"
    while :; do
        if curl -fsS --max-time 1 "http://127.0.0.1:${port}/" >/dev/null 2>&1; then
            return 0
        fi
        now="$(date +%s)"
        if [ $((now - start)) -ge "$timeout" ]; then
            return 1
        fi
        sleep 0.5
    done
}

# fetch_compressed <url> <accept-encoding> [extra curl args ...]
# Sends the request with the given Accept-Encoding, streams the raw response
# body to stdout (i.e. caller can pipe to `zstd -d`). Does NOT decode for the
# caller — that lets the caller assert on byte-identical decompression.
# Note: we deliberately do NOT pass `--compressed` so curl leaves the body
# encoded for the caller to decompress and verify.
fetch_compressed() {
    local url="$1"
    local enc="$2"
    shift 2
    curl -sS -H "Accept-Encoding: ${enc}" "$@" "$url"
}

# fetch_headers <url> <accept-encoding> [extra curl args ...]
# Returns raw response headers (one per line, no body).
fetch_headers() {
    local url="$1"
    local enc="$2"
    shift 2
    curl -sS -D - -o /dev/null -H "Accept-Encoding: ${enc}" "$@" "$url"
}

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

# assert_decompresses_to <bytes-file> <expected-size>
# Pipes the file through `zstd -d` and asserts decoded size equals expected.
assert_decompresses_to() {
    local file="$1"
    local expected="$2"
    local actual
    actual="$(zstd -dc -- "$file" | wc -c | tr -d ' ')"
    if [ "$actual" != "$expected" ]; then
        echo "assert_decompresses_to: expected ${expected} bytes got ${actual}" >&2
        return 1
    fi
}

# assert_eq <label> <actual> <expected>
assert_eq() {
    local label="$1"
    local actual="$2"
    local expected="$3"
    if [ "$actual" != "$expected" ]; then
        echo "assert_eq: ${label}: expected [${expected}] got [${actual}]" >&2
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
