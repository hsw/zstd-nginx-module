#!/bin/bash
# infinite-loop.sh — regression for PR #23 / Task 4.
#
# Background: when an upstream advertises Content-Length: N but sends fewer
# than N bytes and then closes the connection, the original filter looped
# forever waiting for the missing bytes, pegging the worker CPU at 100%. The
# fix is to break out when the upstream signals EOF before the declared length.
#
# Coverage:
#   1. python fixture server on :9000 announces Content-Length: 2, sends "a",
#      then closes the socket (1 byte short).
#   2. nginx proxies /origin/loop to it; curl with Accept-Encoding: zstd
#      must return within ~5s with a 5xx (or curl error 18 / 56 — short read).
#   3. After the curl times out / completes, sample worker CPU time over 2s;
#      delta must stay below 50 ms (i.e. NOT spinning).
#
# Runs inside the container.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_common.sh"

LABEL="infinite-loop"
TMPDIR="$(mktemp -d)"
FIXTURE_PID=""

cleanup() {
    if [ -n "$FIXTURE_PID" ]; then
        kill "$FIXTURE_PID" >/dev/null 2>&1 || true
        wait "$FIXTURE_PID" 2>/dev/null || true
    fi
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

# Python fixture: TCP server that lies about Content-Length. Returns
# Content-Length: 2 in the response header but sends only "a" before closing
# the socket. The original module spun on the missing byte forever.
write_fixture() {
    cat > "${TMPDIR}/fixture.py" <<'PY'
import socket, sys, threading

def handle(c):
    try:
        # Drain request headers — nginx is content with anything HTTP/1.1.
        buf = b""
        c.settimeout(2)
        while b"\r\n\r\n" not in buf:
            chunk = c.recv(4096)
            if not chunk:
                break
            buf += chunk
        # Reply with Content-Length: 2 but send only "a" and close — short read.
        c.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: 2\r\n"
            b"Connection: close\r\n"
            b"\r\n"
            b"a"
        )
    finally:
        try: c.shutdown(socket.SHUT_RDWR)
        except OSError: pass
        c.close()

s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", 9000))
s.listen(8)
sys.stdout.write("ready\n"); sys.stdout.flush()
while True:
    c, _ = s.accept()
    threading.Thread(target=handle, args=(c,), daemon=True).start()
PY
}

start_fixture() {
    write_fixture
    python3 "${TMPDIR}/fixture.py" > "${TMPDIR}/fixture.log" 2>&1 &
    FIXTURE_PID=$!
    # Wait for "ready" line.
    local i=0
    while ! grep -q ready "${TMPDIR}/fixture.log" 2>/dev/null; do
        i=$((i + 1))
        if [ "$i" -ge 30 ]; then
            echo "fixture server did not become ready" >&2
            return 1
        fi
        sleep 0.1
    done
}

render_conf
start_fixture
start_local_nginx

# Locate the worker pid so we can measure its CPU time. Under Docker Desktop
# on macOS, Linux processes run under Rosetta emulation and `pgrep -f` matches
# `/run/rosetta/rosetta /usr/sbin/nginx nginx` rather than the Linux-native
# `nginx: worker process` cmdline form. We discover the worker by finding the
# nginx master via the pidfile, then taking its first child.
MASTER_PID="$(cat /tmp/nginx.pid 2>/dev/null || true)"
if [ -z "$MASTER_PID" ]; then
    echo "could not find nginx master pid (no /tmp/nginx.pid)" >&2
    exit 1
fi
# Workers are children of the master. /proc/<pid>/task/<pid>/children is the
# canonical Linux source-of-truth (works under Rosetta too because /proc is
# native to the Linux guest).
WORKER_PID="$(awk '{print $1}' "/proc/${MASTER_PID}/task/${MASTER_PID}/children" 2>/dev/null || true)"
if [ -z "$WORKER_PID" ]; then
    # Fallback: any process whose ppid is the master.
    WORKER_PID="$(ps -e -o pid=,ppid= 2>/dev/null | awk -v m="$MASTER_PID" '$2 == m {print $1; exit}')"
fi
if [ -z "$WORKER_PID" ]; then
    echo "could not find nginx worker pid (master=${MASTER_PID})" >&2
    ps -e -o pid,ppid,cmd | grep -i nginx >&2 || true
    exit 1
fi

cpu_jiffies() {
    # Field 14 (utime) + field 15 (stime) from /proc/<pid>/stat — jiffies (clk).
    # Adding to avoid double-counting across sample boundaries.
    awk '{print $14 + $15}' "/proc/${WORKER_PID}/stat" 2>/dev/null || echo 0
}

# Drive the request. We expect curl to error out (28=timeout, 18=partial, 52=
# empty reply, 56=recv error) OR return a 5xx. Any of those is acceptable —
# what MUST be true is that the call returns within ~5 seconds.
START_TS="$(date +%s)"
HTTP_CODE="$(curl -sS -o /dev/null -w '%{http_code}' \
    -H "Accept-Encoding: zstd" \
    --max-time 5 \
    "http://127.0.0.1:8080/origin/" 2>/tmp/curl.err || true)"
ELAPSED=$(( $(date +%s) - START_TS ))

if [ "$ELAPSED" -ge 8 ]; then
    _log_fail "${LABEL}/no-hang" "curl took ${ELAPSED}s (must be <8s)"
    exit 1
fi
_log_pass "${LABEL}/no-hang (code=${HTTP_CODE}, elapsed=${ELAPSED}s)"

# Now sample worker CPU over 2 seconds. The fix means the worker drops back to
# idle as soon as the upstream closes; the bug means it spins. Threshold is in
# clock ticks: 100 ticks/sec on Linux ⇒ 50 ms = 5 ticks. We give a generous
# 20-tick budget (200 ms) to absorb container scheduling jitter under qemu.
CLK_TCK="$(getconf CLK_TCK 2>/dev/null || echo 100)"
BEFORE="$(cpu_jiffies)"
sleep 2
AFTER="$(cpu_jiffies)"
DELTA=$((AFTER - BEFORE))
DELTA_MS=$(( DELTA * 1000 / CLK_TCK ))

# 200 ms is a generous ceiling under emulation. A spinning worker would burn
# ~2000 ms in a 2-second window.
if [ "$DELTA_MS" -gt 200 ]; then
    _log_fail "${LABEL}/no-spin" \
        "worker CPU delta ${DELTA_MS}ms over 2s (must be <=200ms)"
    exit 1
fi
_log_pass "${LABEL}/no-spin (delta=${DELTA_MS}ms over 2s)"

echo "${LABEL}: pass=2 fail=0"
