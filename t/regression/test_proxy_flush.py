"""Flush-promotion regression for chunked / SSE / Upgrade upstream patterns.

Background — production reports on the tokers/zstd-nginx-module#23 thread:
  * mklooss 2025-03-20  — Varnish chunked + proxy_buffering on → error with
                          PR #23 patch (i.e. step1's `dc951f8`); works
                          without the patch OR with proxy_buffering off.
  * Stensel8 2026-01-11 — HomeAssistant WebSocket via `Connection: Upgrade`
                          + proxy_buffering off + HTTP/2+HTTP/3 → worker
                          freeze (nginx 1.29.3).
  * lowkeypriority 2026-02-02 — same as Stensel8.

The four "production-shape" sub-tests (chunked-on/off, sse, upgrade) map
to four upstream patterns the bug manifests under. On `stable` HEAD they
ALREADY fail (ReadTimeoutError / hang) — i.e. they discriminate the bug
without needing the additional small-chunk escalation tiers documented
in the migration plan.

The added `chunked-off-tiny` sub-test below is a focused, tighter
production-shape repro: a 50-chunk × 200-byte schedule with 50 ms inter-
chunk gap and a 400 ms TTFB budget. It is wrapped with
`pytest.mark.xfail(strict=True)` until commit 3 retires the action state
machine and adopts ngx_brotli's per-call-op pattern. strict=True ensures
that if the test goes PASS prematurely (e.g. an unintended baseline fix
or overfit discovery params), pytest treats it as a failure.

The Python TCP fixture server is started by a module-scoped pytest
fixture so the threaded handler thread doesn't leak between tests.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from dataclasses import dataclass
from typing import Iterator

import pytest
import requests

from conftest import CONF_PATH, render_template, start_nginx, stop_nginx

FIXTURE_PORT = 9003  # distinct from test_filter_eligibility (9000), test_websocket (9001), test_http2_proxy_flush (9002), test_infinite_loop (9004)
NGINX_URL = "http://127.0.0.1:8080"

# Each handler writes the bytes it sent into this dict so the test can
# byte-compare the decompressed client response against ground truth.
GROUND_TRUTH: dict[str, bytes] = {}
GROUND_TRUTH_LOCK = threading.Lock()


def _send_chunked(c: socket.socket) -> None:
    """6 chunks * ~10 KiB random tail, 200 ms gap. Total upstream window
    ~1.2 s, total body ~60 KiB — bigger than nginx's default
    proxy_buffer_size (4-8 KiB) so the chunks fragment into multiple chain
    links when proxy_buffering is off, but still well under
    ZSTD_CStreamInSize (~128 KiB) so the bug isn't masked by libzstd's
    natural spill.

    Ground truth is precomputed and recorded BEFORE the first send so the
    test thread never observes a stale/None entry under scheduling jitter."""
    chunks = [f"chunk-{i:02d}-".encode() + os.urandom(10240) for i in range(6)]
    full = b"".join(chunks)
    with GROUND_TRUTH_LOCK:
        GROUND_TRUTH["chunked"] = full
    c.sendall(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/plain\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
    )
    for chunk in chunks:
        c.sendall(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
        time.sleep(0.2)
    c.sendall(b"0\r\n\r\n")


def _send_chunked_tiny(c: socket.socket) -> None:
    """50 chunks * 200 bytes random tail, 50 ms gap. Total upstream window
    ~2.5 s, total body ~10 KiB. Tier-1 production-shape repro per the
    migration plan: small chunks where libzstd swallows input without
    immediate spill (rc==0), so the legacy action state machine never
    promotes COMPRESS → FLUSH and bytes stay buffered until upstream
    close. Healthy filter (per-call-op) emits each chunk on b->flush=1
    within ~first-chunk-arrival latency (~50-100 ms).

    Ground truth is precomputed and recorded BEFORE the first send so the
    test thread never observes a stale/None entry under scheduling jitter."""
    chunks = [f"tiny-{i:02d}-".encode() + os.urandom(200) for i in range(50)]
    full = b"".join(chunks)
    with GROUND_TRUTH_LOCK:
        GROUND_TRUTH["chunked-tiny"] = full
    c.sendall(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/plain\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
    )
    for chunk in chunks:
        c.sendall(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
        time.sleep(0.05)
    c.sendall(b"0\r\n\r\n")


def _send_sse(c: socket.socket) -> None:
    """8 SSE events with 200 ms gap. Each event ~512 bytes — realistic
    SSE traffic shape, total upstream window ~1.6 s.

    Ground truth is precomputed and recorded BEFORE the first send so the
    test thread never observes a stale/None entry under scheduling jitter."""
    events = []
    for i in range(8):
        payload = os.urandom(240).hex()
        events.append(f"event: msg\ndata: {i:02d}-{payload}\n\n".encode())
    full = b"".join(events)
    with GROUND_TRUTH_LOCK:
        GROUND_TRUTH["sse"] = full
    c.sendall(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/event-stream\r\n"
        b"Cache-Control: no-cache\r\n"
        b"Connection: close\r\n"
        b"\r\n"
    )
    for evt in events:
        c.sendall(evt)
        time.sleep(0.2)


def _send_upgrade(c: socket.socket) -> None:
    """Stand-in for WebSocket — plain 200 + buffered body. Real WebSocket
    framing needs the `websockets` library (V2). The point here is to
    exercise the zstd filter under Upgrade-shaped request headers, not the
    framing protocol itself.

    Ground truth is recorded BEFORE the send so the test thread never
    observes a stale/None entry under scheduling jitter."""
    body = b"upgrade-stand-in-body-" + b"y" * 200 + b"\n"
    with GROUND_TRUTH_LOCK:
        GROUND_TRUTH["upgrade"] = body
    c.sendall(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n"
        b"\r\n" + body
    )


def _handle(c: socket.socket) -> None:
    try:
        c.settimeout(2)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = c.recv(4096)
            if not chunk:
                break
            buf += chunk
        # Parse request-line path.
        try:
            path = buf.split(b"\r\n", 1)[0].decode("latin-1").split(" ")[1]
        except (IndexError, UnicodeDecodeError):
            path = ""
        if path.startswith("/chunked-tiny"):
            _send_chunked_tiny(c)
        elif path.startswith("/chunked"):
            _send_chunked(c)
        elif path.startswith("/sse"):
            _send_sse(c)
        elif path.startswith("/upgrade"):
            _send_upgrade(c)
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


def _server_loop(sock: socket.socket, stop_event: threading.Event) -> None:
    sock.settimeout(0.5)
    while not stop_event.is_set():
        try:
            c, _ = sock.accept()
        except socket.timeout:
            continue
        threading.Thread(target=_handle, args=(c,), daemon=True).start()


@pytest.fixture(scope="module")
def upstream_fixture() -> Iterator[None]:
    """Module-scoped: one TCP fixture server on FIXTURE_PORT for the whole
    test_proxy_flush module. Started before any sub-test, torn down at
    module exit."""
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", FIXTURE_PORT))
    sock.listen(16)
    stop = threading.Event()
    thread = threading.Thread(
        target=_server_loop, args=(sock, stop), daemon=True
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2)
        sock.close()


EXTRA_LOCATIONS = f"""
    # Sub-test chunked-on: proxy_buffering on (default).
    location /chunked-on/ {{
        proxy_pass http://127.0.0.1:{FIXTURE_PORT}/chunked;
        proxy_http_version 1.1;
    }}
    # Sub-test chunked-off: proxy_buffering off, chunks reach filter with
    # b->flush=1 each.
    location /chunked-off/ {{
        proxy_pass http://127.0.0.1:{FIXTURE_PORT}/chunked;
        proxy_http_version 1.1;
        proxy_buffering off;
    }}
    # Sub-test sse: same flush-per-event pattern but no Content-Length.
    location /sse/ {{
        proxy_pass http://127.0.0.1:{FIXTURE_PORT}/sse;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_read_timeout 30s;
    }}
    # Sub-test upgrade: WebSocket stand-in headers; upstream returns
    # buffered body (real WebSocket framing is V2).
    location /upgrade/ {{
        proxy_pass http://127.0.0.1:{FIXTURE_PORT}/upgrade;
        proxy_http_version 1.1;
        proxy_set_header Connection "Upgrade";
        proxy_set_header Upgrade websocket;
        proxy_buffering off;
    }}
    # Sub-test chunked-off-tiny: Tier-1 production-shape repro — small
    # chunks (50 × 200 B, 50 ms gap) where the action state-machine bug
    # prevents COMPRESS → FLUSH promotion when libzstd swallows input
    # without spilling (rc==0). xfail (strict=True) on stable HEAD;
    # closed by commit 3 (per-call-op pattern).
    location /chunked-off-tiny/ {{
        proxy_pass http://127.0.0.1:{FIXTURE_PORT}/chunked-tiny;
        proxy_http_version 1.1;
        proxy_buffering off;
    }}
"""


@pytest.fixture(scope="module")
def nginx_with_proxy_locations(upstream_fixture) -> Iterator[str]:
    """Module-scoped nginx with the four proxy-flush locations rendered
    in. Replaces the default `nginx` fixture for this test file because
    we need custom locations; the default fixture doesn't parameterise."""
    stop_nginx()
    render_template(extra_locations=EXTRA_LOCATIONS.strip())
    start_nginx()
    try:
        yield NGINX_URL
    finally:
        stop_nginx()


@dataclass
class FlushCase:
    label: str
    path: str
    truth_key: str
    # Max time for the whole request — hang ceiling, NOT latency assertion.
    max_time_s: float = 10.0
    # Max TTFB in milliseconds. 0 disables the assertion. The bug behaviour
    # is "bytes accumulate until upstream closes" → TTFB ≈ upstream window
    # (~1.2-1.6 s) instead of ~first-chunk-arrival (~200 ms). 600 ms is the
    # goldilocks: well above healthy TTFB, well below upstream close.
    max_ttfb_ms: float = 0


CASES = [
    # chunked-on: proxy_buffering on. nginx core coalesces upstream chunks
    # before they reach the body filter, so latency masking is too strong
    # to make TTFB discriminative. Byte-equality + hang ceiling only.
    FlushCase("chunked-on", "/chunked-on/", "chunked", max_ttfb_ms=0),
    # chunked-off: proxy_buffering off. Each chunk reaches body filter
    # with b->flush=1. On stable HEAD this fails (ReadTimeoutError, hang)
    # — production-shape repro of the flush-promotion bug. Healthy filter
    # emits each chunk on b->flush=1; TTFB ≤ 600 ms.
    FlushCase("chunked-off", "/chunked-off/", "chunked", max_ttfb_ms=600),
    FlushCase("sse", "/sse/", "sse", max_ttfb_ms=600),
    FlushCase("upgrade", "/upgrade/", "upgrade", max_ttfb_ms=0),
    # chunked-off-tiny: Tier-1 focused production-shape repro per the
    # migration plan. 50 × 200 B chunks, 50 ms gap, TTFB budget 400 ms.
    # The existing chunked-off/sse/upgrade cases already discriminate the
    # bug (they hang on stable HEAD); this tighter variant is the
    # canonical small-chunk repro for the action-machine flush-promotion
    # bug from tokers/zstd-nginx-module#23 (mklooss / Stensel8 /
    # lowkeypriority). xfail (strict=True): goes PASS only after commit
    # 3 retires the action state machine and adopts ngx_brotli's per-
    # call-op pattern. strict=True flags any premature pass as failure.
    pytest.param(
        FlushCase(
            "chunked-off-tiny", "/chunked-off-tiny/", "chunked-tiny",
            max_ttfb_ms=400,
        ),
        marks=pytest.mark.xfail(
            strict=True,
            reason=(
                "action-machine flush-promotion bug "
                "(tokers/zstd-nginx-module#23); closed by per-call-op "
                "refactor in commit 3 of v2/compress-stream2"
            ),
        ),
    ),
]


def _case_id(c):
    """Extract label from either a bare FlushCase or a pytest.param-wrapped one."""
    if isinstance(c, FlushCase):
        return c.label
    # pytest.param: .values is the args tuple, first elem is the FlushCase
    return c.values[0].label


@pytest.mark.parametrize("case", CASES, ids=[_case_id(c) for c in CASES])
def test_proxy_flush(nginx_with_proxy_locations, case: FlushCase, tmp_path):
    """Issue a single upstream-driven request, byte-compare the decoded
    response against ground truth recorded by the fixture handler.

    Reads the raw zstd frame off the wire via `r.raw.stream(decode_content
    =False)` — bypassing urllib3's auto-zstd decoder (registered because
    python3-zstandard is installed system-wide on Ubuntu 24.04+). If we
    let urllib3 decode, we'd be comparing decoded plaintext against truth,
    losing the assertion that the response is actually a valid zstd frame
    (and not, e.g., uncompressed because Content-Encoding negotiation
    silently failed).

    TTFB measurement: time from request start to first raw-zstd byte
    arriving via the streaming reader. Production bug ("Stensel8 freeze")
    manifests as TTFB ≈ total upstream window (no progressive flush);
    healthy filter has TTFB ≈ first-chunk-arrival (~200 ms).
    """
    import subprocess
    url = nginx_with_proxy_locations + case.path
    start = time.monotonic()
    # Explicit Accept-Encoding: zstd, no auto-decode. stream=True returns
    # immediately after headers — body is read lazily via r.raw.stream.
    r = requests.get(
        url,
        headers={"Accept-Encoding": "zstd"},
        timeout=case.max_time_s,
        stream=True,
    )
    ttfb_ms: float | None = None
    body = bytearray()
    try:
        # r.raw is the underlying urllib3 HTTPResponse. .stream() yields
        # raw bytes off the connection with decode_content controllable.
        for chunk in r.raw.stream(amt=4096, decode_content=False):
            if chunk:
                if ttfb_ms is None:
                    ttfb_ms = (time.monotonic() - start) * 1000.0
                body.extend(chunk)
    finally:
        r.close()
    total_ms = (time.monotonic() - start) * 1000.0

    assert r.headers.get("Content-Encoding") == "zstd", (
        f"[{case.label}] Content-Encoding={r.headers.get('Content-Encoding')!r}, "
        f"expected zstd"
    )
    assert bytes(body[:4]) == b"\x28\xb5\x2f\xfd", (
        f"[{case.label}] response missing zstd magic; hex={bytes(body[:16]).hex()} "
        f"(decode_content=False bypass should have given raw frame bytes)"
    )

    # Decompress and byte-compare against the upstream truth bytes.
    zst_path = tmp_path / f"{case.label}.zst"
    zst_path.write_bytes(bytes(body))
    dec = subprocess.run(
        ["zstd", "-dc", str(zst_path)],
        capture_output=True, check=False,
    )
    assert dec.returncode == 0, (
        f"[{case.label}] zstd -d failed: {dec.stderr.decode(errors='replace')}"
    )

    with GROUND_TRUTH_LOCK:
        truth = GROUND_TRUTH.get(case.truth_key)
    assert truth is not None, (
        f"[{case.label}] fixture handler did not record ground truth for "
        f"key={case.truth_key!r}"
    )
    assert dec.stdout == truth, (
        f"[{case.label}] decoded differs from upstream: "
        f"truth={len(truth)}B vs dec={len(dec.stdout)}B"
    )

    if case.max_ttfb_ms > 0:
        assert ttfb_ms is not None and ttfb_ms <= case.max_ttfb_ms, (
            f"[{case.label}] ttfb={ttfb_ms!r}ms exceeds {case.max_ttfb_ms}ms "
            f"budget (total={total_ms:.0f}ms) — flush promotion appears not "
            f"to be working; bytes accumulated until upstream close"
        )
