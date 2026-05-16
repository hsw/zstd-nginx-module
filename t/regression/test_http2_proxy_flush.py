"""HTTP/2 axis on the flush-promotion regression (bug #4b).

Companion to test_proxy_flush.py. Same upstream fixture and sub-tests
(chunked-on / chunked-off / sse / upgrade), but the client speaks HTTP/2
cleartext (h2c) to nginx. Upstream side stays HTTP/1.1 — that's the
Stensel8 production shape: client on HTTP/2+HTTP/3, upstream (Home-
Assistant) on plain HTTP/1.1. nginx translates between them.

Why this matters: HTTP/2 strict DATA frame accounting can surface
flush-promotion bugs that HTTP/1.1 chunked encoding papers over. The
production reports from Stensel8 and lowkeypriority involved HTTP/2 on
the client side; our HTTP/1.1-only proxy_flush test catches the bytes-
accumulated-until-upstream-close timeout but doesn't exercise the H2
framing path.

Uses curl --http2-prior-knowledge for the client — matches the existing
test_h2_truncation.py style, avoids pulling httpx into the docker image.

The four sub-tests mirror test_proxy_flush.py exactly. Expected outcomes
on master baseline (test1): same 3 deterministic timeouts as the H1.1
case (chunked-off, sse, upgrade). HTTP/2 may surface additional
truncation or stream-reset symptoms beyond the timeout — those are
worth catching too.
"""

from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Iterator

import pytest

from conftest import (
    BASE_URL,
    _nginx_has_compat,
    render_template,
    start_nginx,
    stop_nginx,
    zstd_decompress,
)

FIXTURE_PORT = 9002  # distinct from test_proxy_flush.py's 9000 / test_websocket.py's 9001

# Module-local ground-truth dict (separate from test_proxy_flush.py's so
# parallel module execution doesn't interfere).
GROUND_TRUTH: dict[str, bytes] = {}
GROUND_TRUTH_LOCK = threading.Lock()


def _send_chunked(c: socket.socket) -> None:
    """6 chunks × ~10 KiB random tail, 200 ms gap. Total upstream window
    ~1.2 s, total body ~60 KiB."""
    c.sendall(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/plain\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
    )
    full = bytearray()
    for i in range(6):
        chunk = f"chunk-{i:02d}-".encode() + os.urandom(10240)
        full.extend(chunk)
        c.sendall(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
        time.sleep(0.2)
    c.sendall(b"0\r\n\r\n")
    with GROUND_TRUTH_LOCK:
        GROUND_TRUTH["chunked"] = bytes(full)


def _send_sse(c: socket.socket) -> None:
    c.sendall(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/event-stream\r\n"
        b"Cache-Control: no-cache\r\n"
        b"Connection: close\r\n"
        b"\r\n"
    )
    full = bytearray()
    for i in range(8):
        payload = os.urandom(240).hex()
        evt = f"event: msg\ndata: {i:02d}-{payload}\n\n".encode()
        full.extend(evt)
        c.sendall(evt)
        time.sleep(0.2)
    with GROUND_TRUTH_LOCK:
        GROUND_TRUTH["sse"] = bytes(full)


def _send_upgrade(c: socket.socket) -> None:
    body = b"upgrade-stand-in-body-" + b"y" * 200 + b"\n"
    c.sendall(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n"
        b"\r\n" + body
    )
    with GROUND_TRUTH_LOCK:
        GROUND_TRUTH["upgrade"] = body


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
    """Module-scoped nginx with http2 on; + 4 proxy-flush locations."""
    load_modules = (
        "load_module modules/ngx_http_zstd_filter_module.so;"
        if _nginx_has_compat() else ""
    )
    stop_nginx()
    render_template(
        extra_directives="http2 on;",
        extra_locations=EXTRA_LOCATIONS.strip(),
        load_modules=load_modules,
    )
    start_nginx()
    try:
        yield BASE_URL
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
def test_http2_proxy_flush(nginx_h2, case: FlushCase, tmp_path):
    """Drive a single HTTP/2 GET via curl --http2-prior-knowledge, parse
    its `-w` timing output for TTFB/total, decompress body, byte-compare
    against the upstream ground truth.

    curl writes timing JSON to a separate file via `-w '@/path'` style is
    overkill — we use the simple "time1 time2" format consumed by awk.
    """
    url = nginx_h2 + case.path
    body_file = tmp_path / f"{case.label}.body"
    timing_file = tmp_path / f"{case.label}.timing"

    r = subprocess.run(
        [
            "curl",
            "-sS",
            "--http2-prior-knowledge",
            "-H", "Accept-Encoding: zstd",
            "--max-time", str(case.max_time_s),
            "-w", "%{time_starttransfer} %{time_total} %{http_version}\n",
            "-o", str(body_file),
            url,
        ],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        pytest.fail(
            f"[{case.label}] curl rc={r.returncode}, stderr={r.stderr!r}"
        )
    timing_file.write_text(r.stdout)
    # "time_starttransfer time_total http_version"
    parts = r.stdout.strip().split()
    assert len(parts) == 3, f"unexpected curl -w output: {r.stdout!r}"
    ttfb_ms = float(parts[0]) * 1000.0
    total_ms = float(parts[1]) * 1000.0
    http_version = parts[2]
    assert http_version == "2", (
        f"[{case.label}] http_version={http_version!r} — curl did not "
        f"negotiate HTTP/2 (server may have downgraded to 1.1)"
    )

    body = body_file.read_bytes()
    assert body[:4] == b"\x28\xb5\x2f\xfd", (
        f"[{case.label}] response missing zstd magic; hex={body[:16].hex()} "
        f"(may have been served without Content-Encoding due to negotiation)"
    )

    decoded = zstd_decompress(body)

    with GROUND_TRUTH_LOCK:
        truth = GROUND_TRUTH.get(case.truth_key)
    assert truth is not None, (
        f"[{case.label}] fixture handler did not record truth for {case.truth_key!r}"
    )
    assert decoded == truth, (
        f"[{case.label}] decoded differs from upstream: "
        f"truth={len(truth)}B vs dec={len(decoded)}B "
        f"(HTTP/2 strict framing may have truncated a DATA frame)"
    )

    if case.max_ttfb_ms > 0:
        assert ttfb_ms <= case.max_ttfb_ms, (
            f"[{case.label}] ttfb={ttfb_ms:.0f}ms exceeds {case.max_ttfb_ms}ms "
            f"budget (total={total_ms:.0f}ms) — flush-promotion gap under HTTP/2"
        )
