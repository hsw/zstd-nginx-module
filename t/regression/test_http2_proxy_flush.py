"""HTTP/2 axis on the flush-promotion regression (bug #4b).

Companion to test_proxy_flush.py. Same Python TCP upstream fixture and
sub-tests (chunked-on / chunked-off / sse / upgrade), but the client
speaks HTTP/2 cleartext (h2c) to nginx. Upstream side stays HTTP/1.1 —
that's Stensel8's production shape: client on HTTP/2/HTTP/3, upstream
(HomeAssistant) on HTTP/1.1. nginx translates between them.

Why this matters: HTTP/2 strict DATA frame accounting can surface
flush-promotion bugs that HTTP/1.1 chunked encoding papers over.

Uses httpx with http2=True (venv-pinned). Reads raw bytes via
`Response.iter_raw()` to bypass httpx's auto-zstd decoder (would
otherwise inflate the body silently). TTFB measurement: time from
request start to first iter_raw chunk arrival — same definition as
curl's `time_starttransfer`.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from dataclasses import dataclass
from typing import Iterator

import httpx
import pytest

from conftest import (
    _nginx_has_compat,
    render_template,
    start_nginx,
    stop_nginx,
    zstd_decompress,
)

FIXTURE_PORT = 9002  # distinct from test_proxy_flush.py's 9000 / test_websocket.py's 9001

# httpx 0.28 negotiates HTTP/2 only via TLS+ALPN. See test_h2_truncation.py
# for the same setup explanation.
H2_BASE_URL = "https://127.0.0.1:8443"
EXTRA_SERVER_TLS = """
        listen 8443 ssl;
        ssl_certificate /etc/nginx/test-cert.pem;
        ssl_certificate_key /etc/nginx/test-key.pem;
"""

GROUND_TRUTH: dict[str, bytes] = {}
GROUND_TRUTH_LOCK = threading.Lock()


def _send_chunked(c: socket.socket) -> None:
    """6 chunks × ~10 KiB random tail, 200 ms gap. Total upstream window
    ~1.2 s, total body ~60 KiB.

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


def _send_sse(c: socket.socket) -> None:
    """Ground truth is precomputed and recorded BEFORE the first send so
    the test thread never observes a stale/None entry under scheduling
    jitter."""
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
    """Ground truth is recorded BEFORE the send so the test thread never
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
        try:
            path = buf.split(b"\r\n", 1)[0].decode("latin-1").split(" ")[1]
        except (IndexError, UnicodeDecodeError):
            path = ""
        if path.startswith("/chunked"):
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


def _has_http2() -> bool:
    import subprocess
    out = subprocess.run(
        ["nginx", "-V"], capture_output=True, text=True, check=False,
    )
    return "--with-http_v2_module" in (out.stdout + out.stderr)


pytestmark = pytest.mark.skipif(
    not _has_http2(), reason="nginx built without --with-http_v2_module",
)


EXTRA_LOCATIONS = f"""
    location /chunked-on/ {{
        proxy_pass http://127.0.0.1:{FIXTURE_PORT}/chunked;
        proxy_http_version 1.1;
    }}
    location /chunked-off/ {{
        proxy_pass http://127.0.0.1:{FIXTURE_PORT}/chunked;
        proxy_http_version 1.1;
        proxy_buffering off;
    }}
    location /sse/ {{
        proxy_pass http://127.0.0.1:{FIXTURE_PORT}/sse;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_read_timeout 30s;
    }}
    location /upgrade/ {{
        proxy_pass http://127.0.0.1:{FIXTURE_PORT}/upgrade;
        proxy_http_version 1.1;
        proxy_set_header Connection "Upgrade";
        proxy_set_header Upgrade websocket;
        proxy_buffering off;
    }}
"""


@pytest.fixture(scope="module")
def upstream_fixture() -> Iterator[None]:
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


@pytest.fixture(scope="module")
def nginx_h2(upstream_fixture) -> Iterator[str]:
    load_modules = (
        "load_module modules/ngx_http_zstd_filter_module.so;"
        if _nginx_has_compat() else ""
    )
    stop_nginx()
    render_template(
        extra_directives="http2 on;",
        extra_server=EXTRA_SERVER_TLS,
        extra_locations=EXTRA_LOCATIONS.strip(),
        load_modules=load_modules,
    )
    start_nginx()
    try:
        yield H2_BASE_URL
    finally:
        stop_nginx()


@dataclass
class FlushCase:
    label: str
    path: str
    truth_key: str
    max_time_s: float = 10.0
    max_ttfb_ms: float = 0  # 0 = disabled


CASES = [
    FlushCase("chunked-on", "/chunked-on/", "chunked", max_ttfb_ms=0),
    FlushCase("chunked-off", "/chunked-off/", "chunked", max_ttfb_ms=600),
    FlushCase("sse", "/sse/", "sse", max_ttfb_ms=600),
    FlushCase("upgrade", "/upgrade/", "upgrade", max_ttfb_ms=0),
]


@pytest.mark.parametrize("case", CASES, ids=[c.label for c in CASES])
def test_http2_proxy_flush(nginx_h2, case: FlushCase):
    """HTTP/2 GET via httpx + iter_raw, asserts byte-equality against the
    upstream truth bytes and (for the latency-sensitive sub-tests) a TTFB
    budget. On master baseline the buffering-off cases time out — same
    bug #4b root cause as the H1.1 version, different transport."""
    url = nginx_h2 + case.path
    body = bytearray()
    ttfb_ms: float | None = None
    start = time.monotonic()

    with httpx.Client(http2=True, verify=False, timeout=case.max_time_s) as client:
        with client.stream(
            "GET", url, headers={"Accept-Encoding": "zstd"},
        ) as r:
            assert r.http_version == "HTTP/2", (
                f"[{case.label}] http_version={r.http_version!r} — "
                f"httpx did not negotiate HTTP/2"
            )
            assert r.headers.get("content-encoding") == "zstd", (
                f"[{case.label}] Content-Encoding="
                f"{r.headers.get('content-encoding')!r}"
            )
            for chunk in r.iter_raw():
                if chunk:
                    if ttfb_ms is None:
                        ttfb_ms = (time.monotonic() - start) * 1000.0
                    body.extend(chunk)

    total_ms = (time.monotonic() - start) * 1000.0

    assert bytes(body[:4]) == b"\x28\xb5\x2f\xfd", (
        f"[{case.label}] missing zstd magic; first 16B="
        f"{bytes(body[:16]).hex()}"
    )

    decoded = zstd_decompress(bytes(body))
    with GROUND_TRUTH_LOCK:
        truth = GROUND_TRUTH.get(case.truth_key)
    assert truth is not None, (
        f"[{case.label}] no ground truth for {case.truth_key!r}"
    )
    assert decoded == truth, (
        f"[{case.label}] decoded differs: truth={len(truth)}B vs "
        f"dec={len(decoded)}B (HTTP/2 frame truncation?)"
    )

    if case.max_ttfb_ms > 0:
        assert ttfb_ms is not None and ttfb_ms <= case.max_ttfb_ms, (
            f"[{case.label}] ttfb={ttfb_ms!r}ms exceeds "
            f"{case.max_ttfb_ms}ms (total={total_ms:.0f}ms) — "
            f"flush-promotion gap under HTTP/2"
        )
