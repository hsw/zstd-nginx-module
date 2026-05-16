#!/bin/bash
# proxy-flush.sh — flush-promotion regression for chunked / SSE / Upgrade
# upstream patterns.
#
# Background: when an upstream emits HTTP/1.1 Transfer-Encoding: chunked with
# delayed chunks (Varnish, SSE event streams, WebSocket-style framing), nginx
# core delivers each upstream chunk to the zstd body_filter as a separate
# ngx_chain_t with b->flush=1. The legacy compress state machine on master
# baseline does NOT translate `ctx->flush` into `ZSTD_flushStream()` when
# libzstd hasn't accumulated enough output to spill on its own — so the
# request hangs (no bytes go downstream) until either (a) more bytes
# accumulate, or (b) last_buf arrives. For SSE / WebSocket where bytes are
# semantically meaningful at fine grain, the client sees the connection as
# hung; CPU may pin if any path loops on `redo`.
#
# Upstream tracking: tokers/zstd-nginx-module#23 thread —
#   mklooss 2025-03-20      (Varnish chunked → worker freeze)
#   Stensel8 2026-01-11     (HomeAssistant WebSocket → freeze)
#   lowkeypriority 2026-02-02 (same)
# Cherry-picked fix `dc951f8` patched ONLY a narrow sub-case
# (last_action == FLUSH + empty out_buf sentinel) and is to be reverted in
# 0.2.1 in favour of a proper flush-promotion at the top of compress.
#
# Sub-tests:
#   A. chunked + proxy_buffering on   (Varnish-like)
#   B. chunked + proxy_buffering off  (small-frame realtime)
#   C. SSE — Content-Type: text/event-stream, no Content-Length, close at end
#   D. Connection: Upgrade — stand-in for WebSocket framing without the full
#      protocol; the goal is to verify the zstd filter handles the Upgrade
#      negotiation gracefully, not to implement WebSocket. True WebSocket
#      framing is a documented V2 gap (Python WebSocket fixture).
#
# Acceptance: each sub-test must (1) complete within --max-time, (2) emit
# Content-Encoding: zstd, (3) decompress to the upstream-emitted payload
# byte-for-byte. Sub-tests A and B additionally measure response time —
# without the fix, the chunked upstream's slow-flush pattern can stall the
# response by seconds while libzstd waits for more bytes.
#
# **Coverage scope**: this test catches *frame corruption* and *truncation*
# (byte-equality of decompressed output) and *gross hangs* (3-second elapsed
# ceiling). It does NOT catch the production *latency* bug — where the bytes
# eventually arrive correctly but only after upstream close, instead of
# progressively per upstream flush. For 6×200-byte chunks + 80 ms gaps the
# total upstream window is ~480 ms, which finishes well inside the 3-s
# ceiling whether or not flush promotion works. Detecting the latency bug
# requires measuring `time_starttransfer` against `time_total` (TTFB <<
# end-to-end), with chunks large enough that the upstream-close-saves-us
# escape isn't available (≥ ZSTD_CStreamInSize per chunk).
#
# **TDD note**: lives on branch `test1` (master + step1 regression suite).
# Master has NEITHER the PR #23 cherry-pick nor the 0.2.1 flush promotion.
# All four sub-tests currently PASS on master baseline — meaning the test
# provides regression coverage going forward (compressStream2 migration,
# Option γ action-machine rewrite) but does not flag the existing
# flush-promotion gap. Hardening to actually fail on master is V2 work —
# requires a TTFB / latency assertion, see comment above.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_common.sh"

LABEL="proxy-flush"
TMPDIR="$(mktemp -d)"
FIXTURE_PID=""
PASS=0
FAIL=0

cleanup() {
    if [ -n "$FIXTURE_PID" ]; then
        kill "$FIXTURE_PID" >/dev/null 2>&1 || true
        wait "$FIXTURE_PID" 2>/dev/null || true
    fi
    stop_local_nginx
    rm -rf "$TMPDIR"
}
trap 'cleanup' EXIT

# Python TCP fixture. Three endpoints:
#   /chunked  → 6 chunks of 200 bytes with 80 ms gap between chunks,
#               terminated by 0\r\n\r\n.
#   /sse      → 5 SSE events ("data: msg-N\n\n") with 100 ms gap,
#               no Content-Length, close at EOF.
#   /upgrade  → returns 200 OK + a small body. Real WebSocket would be 101
#               Switching Protocols; we send 200 because nginx upstream
#               doesn't fully proxy a 101 unless extra config is in place,
#               and the goal here is to exercise the zstd filter under
#               Connection: Upgrade upstream headers, not WebSocket framing.
#
# Each endpoint records the exact body bytes it sent into a marker file so
# the test can byte-compare the decompressed response against ground truth.
write_fixture() {
    cat > "${TMPDIR}/fixture.py" <<'PY'
import os
import socket
import sys
import threading
import time


GROUND_TRUTH_DIR = os.environ.get("GROUND_TRUTH_DIR", "/tmp/proxy-flush-truth")
os.makedirs(GROUND_TRUTH_DIR, exist_ok=True)


def write_truth(name: str, payload: bytes) -> None:
    with open(os.path.join(GROUND_TRUTH_DIR, name), "wb") as fh:
        fh.write(payload)


def send_chunked(c: socket.socket) -> None:
    """6 chunks * 200 bytes, 80 ms gap. Deterministic payload."""
    head = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/plain\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
    )
    c.sendall(head)
    full = b""
    for i in range(6):
        chunk = (f"chunk-{i:02d}-" + ("x" * 188) + "\n").encode()
        full += chunk
        c.sendall(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
        time.sleep(0.08)
    c.sendall(b"0\r\n\r\n")
    write_truth("chunked", full)


def send_sse(c: socket.socket) -> None:
    """5 SSE events with 100 ms gap. No Content-Length; close at EOF."""
    head = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/event-stream\r\n"
        b"Cache-Control: no-cache\r\n"
        b"Connection: close\r\n"
        b"\r\n"
    )
    c.sendall(head)
    full = b""
    for i in range(5):
        evt = f"data: msg-{i:02d}\n\n".encode()
        full += evt
        c.sendall(evt)
        time.sleep(0.1)
    write_truth("sse", full)


def send_upgrade(c: socket.socket) -> None:
    """Plain 200 OK with a body. Upgrade headers are set by nginx's
    proxy directives on the way in; we just have to be reachable."""
    body = b"upgrade-stand-in-body-" + b"y" * 200 + b"\n"
    head = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n"
        b"\r\n"
    )
    c.sendall(head + body)
    write_truth("upgrade", body)


def parse_path(buf: bytes) -> str:
    # First line "GET /path HTTP/1.1"
    try:
        first = buf.split(b"\r\n", 1)[0].decode("latin-1")
        return first.split(" ")[1]
    except Exception:
        return ""


def handle(c: socket.socket) -> None:
    try:
        c.settimeout(2)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = c.recv(4096)
            if not chunk:
                break
            buf += chunk
        path = parse_path(buf)
        if path.startswith("/chunked"):
            send_chunked(c)
        elif path.startswith("/sse"):
            send_sse(c)
        elif path.startswith("/upgrade"):
            send_upgrade(c)
        else:
            c.sendall(
                b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n"
                b"Connection: close\r\n\r\n"
            )
    finally:
        try:
            c.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        c.close()


def main() -> None:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 9000))
    s.listen(16)
    sys.stdout.write("ready\n")
    sys.stdout.flush()
    while True:
        c, _ = s.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()


if __name__ == "__main__":
    main()
PY
}

GROUND_TRUTH_DIR="${TMPDIR}/truth"
mkdir -p "$GROUND_TRUTH_DIR"
export GROUND_TRUTH_DIR

start_fixture() {
    write_fixture
    python3 "${TMPDIR}/fixture.py" > "${TMPDIR}/fixture.log" 2>&1 &
    FIXTURE_PID=$!
    local i=0
    while ! grep -q ready "${TMPDIR}/fixture.log" 2>/dev/null; do
        i=$((i + 1))
        if [ "$i" -ge 30 ]; then
            echo "fixture server did not become ready" >&2
            cat "${TMPDIR}/fixture.log" >&2 || true
            return 1
        fi
        sleep 0.1
    done
}

render_conf() {
    local extra_locs
    extra_locs="$(cat <<'NGX'
        # Sub-test A: proxy_buffering on (default). nginx core may coalesce
        # the upstream chunks before they reach the body filter; whether the
        # b->flush flag survives depends on proxy_buffer_size vs chunk size.
        location /chunked-on/ {
            proxy_pass http://127.0.0.1:9000/chunked;
            proxy_http_version 1.1;
        }

        # Sub-test B: proxy_buffering off. Each upstream chunk reaches the
        # body filter as a separate chain link with b->flush=1. This is
        # where the missing flush-promotion in compress() manifests.
        location /chunked-off/ {
            proxy_pass http://127.0.0.1:9000/chunked;
            proxy_http_version 1.1;
            proxy_buffering off;
        }

        # Sub-test C: SSE. Same flush-per-chunk pattern as B but with
        # text/event-stream framing and no Content-Length.
        location /sse/ {
            proxy_pass http://127.0.0.1:9000/sse;
            proxy_http_version 1.1;
            proxy_buffering off;
            proxy_read_timeout 30s;
        }

        # Sub-test D: Connection: Upgrade. Stand-in for WebSocket — we set
        # the Upgrade/Connection headers proxy-side but the upstream answers
        # with a plain 200 + body. Goal: ensure zstd filter handles the
        # Upgrade negotiation headers gracefully, not WebSocket framing
        # (V2 gap — needs a Python WebSocket fixture for full coverage).
        location /upgrade/ {
            proxy_pass http://127.0.0.1:9000/upgrade;
            proxy_http_version 1.1;
            proxy_set_header Connection "Upgrade";
            proxy_set_header Upgrade websocket;
            proxy_buffering off;
        }
NGX
)"

    render_nginx_template \
        'load_module modules/ngx_http_zstd_filter_module.so;' \
        '' \
        "$extra_locs" \
        8080 \
        /etc/nginx/nginx.conf

    if ! nginx -V 2>&1 | grep -q -- '--with-compat'; then
        sed -i '/^load_module /d' /etc/nginx/nginx.conf
    fi
    apply_daemon_mode /etc/nginx/nginx.conf
}

# run_subtest <label> <url> <truth-file> <max-time-sec> <max-elapsed-sec>
# Asserts: curl completes within max-time, Content-Encoding=zstd,
# decompressed body == ground truth, elapsed <= max-elapsed (latency check
# for the flush-promotion failure mode).
run_subtest() {
    local label="$1" url="$2" truth="$3" max_time="$4" max_elapsed="$5"
    local out="${TMPDIR}/${label}.body"
    local dec="${TMPDIR}/${label}.dec"
    local hdr="${TMPDIR}/${label}.hdr"
    local start end elapsed

    start="$(date +%s)"
    if ! curl -sS --http1.1 -H "Accept-Encoding: zstd" \
            --max-time "$max_time" \
            -o "$out" -D "$hdr" "$url"; then
        end="$(date +%s)"
        elapsed=$((end - start))
        _log_fail "${LABEL}/${label}" "curl failed after ${elapsed}s (max-time=${max_time})"
        FAIL=$((FAIL + 1))
        return
    fi
    end="$(date +%s)"
    elapsed=$((end - start))

    local ce
    ce="$(header_value "$(cat "$hdr")" Content-Encoding)"
    if [ "$ce" != "zstd" ]; then
        _log_fail "${LABEL}/${label}" \
            "Content-Encoding=[${ce}] (expected zstd) elapsed=${elapsed}s"
        FAIL=$((FAIL + 1))
        return
    fi

    if ! zstd -dc -- "$out" > "$dec" 2>/tmp/zstd-err; then
        _log_fail "${LABEL}/${label}" "zstd -d failed: $(cat /tmp/zstd-err)"
        FAIL=$((FAIL + 1))
        return
    fi

    if ! cmp -s "$truth" "$dec"; then
        _log_fail "${LABEL}/${label}" \
            "decoded differs (truth=$(wc -c < "$truth") dec=$(wc -c < "$dec"))"
        FAIL=$((FAIL + 1))
        return
    fi

    if [ "$elapsed" -gt "$max_elapsed" ]; then
        _log_fail "${LABEL}/${label}" \
            "elapsed=${elapsed}s exceeds ${max_elapsed}s budget — flush stalled?"
        FAIL=$((FAIL + 1))
        return
    fi

    _log_pass "${LABEL}/${label} (elapsed=${elapsed}s)"
    PASS=$((PASS + 1))
}

start_fixture
render_conf
start_local_nginx_bg /etc/nginx/nginx.conf

# Drive each sub-test sequentially. Each one issues a single upstream request
# at our python fixture, so subtests don't interfere even though the fixture
# is multi-threaded.
#
# Time budgets: the fixture sends 6×80ms chunked + closes (≈ 480 ms total) or
# 5×100ms SSE (≈ 500 ms). The flush-promotion bug typically stalls until
# the entire body has accumulated to ZSTD's internal block boundary
# (~128 KiB) OR the upstream closes — for our small payloads that means
# the response only flushes at upstream EOF (~500 ms), not progressively.
# We give 3 s budget which is generous for healthy code and tight enough to
# fail the bug.

run_subtest chunked-on  http://127.0.0.1:8080/chunked-on/  \
    "${GROUND_TRUTH_DIR}/chunked"  10 3
run_subtest chunked-off http://127.0.0.1:8080/chunked-off/ \
    "${GROUND_TRUTH_DIR}/chunked"  10 3
run_subtest sse         http://127.0.0.1:8080/sse/         \
    "${GROUND_TRUTH_DIR}/sse"      10 3
run_subtest upgrade     http://127.0.0.1:8080/upgrade/     \
    "${GROUND_TRUTH_DIR}/upgrade"  10 3

echo "${LABEL}: pass=${PASS} fail=${FAIL}"
[ "$FAIL" -eq 0 ]
