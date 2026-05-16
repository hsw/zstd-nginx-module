#!/bin/bash
# _common.sh — shared helpers sourced by every regression script in t/regression/.
# All helpers exit nonzero on failure so the calling script can `set -e`.
#
# Conventions:
#   * Regression scripts render an nginx config and run nginx in-container via
#     start_local_nginx_bg / stop_local_nginx — no separate docker container is
#     started by these helpers.
#   * The test port is hardcoded to 8080 across all scripts. Don't add an
#     override — keep config simple; if isolation across concurrent runs is
#     ever needed, rev all 7 regression scripts at once.

set -uo pipefail

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

# apply_daemon_mode <conf-path> — rewrite the nginx.conf at $1 so the daemon /
# master_process directives match the current harness mode.
#
# Default (ZSTD_REGRESSION_NO_DAEMON unset/empty):
#   * `daemon off;` → `daemon on;` so `nginx -c ...` exits after fork and the
#     regression script can continue (sending requests, then nginx -s stop).
#
# Harness mode (ZSTD_REGRESSION_NO_DAEMON=1):
#   * Keep `daemon off;` AND prepend `master_process off;` (if not already
#     present) so nginx runs as a single in-foreground process. Required by
#     t/valgrind.sh and t/asan.sh: under daemonize, the worker double-forks
#     away from the harness, so neither `valgrind --trace-children` nor
#     `docker exec -e ASAN_OPTIONS … bash regression.sh` captures the worker's
#     leak/UB findings. Single-process foreground keeps the request handler
#     inside the harness.
#
# Callers should follow with `start_local_nginx_bg` (defined below) when in
# harness mode — `nginx -c …` would otherwise block forever with daemon off.
apply_daemon_mode() {
    local conf="${1:-/etc/nginx/nginx.conf}"
    if [ -n "${ZSTD_REGRESSION_NO_DAEMON:-}" ]; then
        # daemon off is already the template default; we only need to add
        # master_process off to collapse master/worker into one process.
        if ! grep -q '^master_process ' "$conf"; then
            # Insert right after the `daemon off;` line so both top-level
            # directives are co-located.
            sed -i '/^daemon off;/a\
master_process off;' "$conf"
        fi
    else
        sed -i 's|^daemon off;|daemon on;|' "$conf"
    fi
}

# start_local_nginx_bg <conf-path> — start nginx so the regression script can
# continue executing requests against it, regardless of ZSTD_REGRESSION_NO_DAEMON.
#
# Default mode: `nginx -c ...` daemonizes and returns immediately.
# Harness mode: `nginx -c ... &` — disowned background job; the caller stops it
# with `stop_local_nginx` at cleanup. Waits for /tmp/nginx.pid (default mode)
# or for the listener to come up (harness mode, since there's no pidfile until
# after listen).
start_local_nginx_bg() {
    local conf="${1:-/etc/nginx/nginx.conf}"
    nginx -c "$conf" -t >/tmp/nginx-t.log 2>&1 || {
        echo "nginx -t failed" >&2
        cat /tmp/nginx-t.log >&2
        return 1
    }
    if [ -n "${ZSTD_REGRESSION_NO_DAEMON:-}" ]; then
        # Background so the regression script keeps running. Route nginx
        # stderr to the SCRIPT's stderr so docker exec captures it in the
        # asan/valgrind driver's per-script log — this is where ASan
        # `==ERROR:` reports and valgrind `definitely lost:` lines surface.
        # If we redirected to /tmp/somefile inside the container, those
        # findings would be invisible to the host-side driver.
        # `nohup` would close our stderr inheritance; do not use it.
        # stdout → stderr; stderr is already inherited from the docker exec.
        nginx -c "$conf" >&2 &
        disown >/dev/null 2>&1 || true
    else
        nginx -c "$conf"
    fi
    local i=0
    while ! curl -fsS --max-time 1 "http://127.0.0.1:8080/" >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -ge 60 ]; then
            echo "nginx did not start within 6s" >&2
            return 1
        fi
        sleep 0.1
    done
}

# nginx_worker_pid <master-pid> — resolve the worker pid driving requests for
# the given master. In single-process mode (master_process off — used under the
# valgrind/asan harness so worker stderr stays attached), there's no separate
# worker: the master process IS the worker. Returns the worker pid on stdout,
# or empty string if nothing was found (caller decides whether that's fatal).
nginx_worker_pid() {
    local master="$1"
    local pid

    pid="$(awk '{print $1}' "/proc/${master}/task/${master}/children" 2>/dev/null || true)"
    if [ -z "$pid" ]; then
        pid="$(ps -e -o pid=,ppid= 2>/dev/null | awk -v m="$master" '$2 == m {print $1; exit}')"
    fi
    if [ -z "$pid" ]; then
        # Single-process mode: master is the worker.
        if ! ps -p "$master" >/dev/null 2>&1; then
            return 0
        fi
        pid="$master"
    fi
    printf '%s\n' "$pid"
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
