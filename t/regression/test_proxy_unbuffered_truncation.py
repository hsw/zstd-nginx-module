"""Regression: silent truncation / zero-size-buf abort when the zstd filter
compresses a proxied response under `proxy_buffering off`.

Ported from eilandert/zstd-nginx-module tools/test_proxy_unbuffered_truncation.py
("bug B": a production incident on deb.myguard.nl where a WordPress vhost with
`proxy_buffering off` served truncated / empty zstd responses). Re-expressed in
our pytest + conftest harness (threaded HTTP backend, raw-wire decode, byte
compare) instead of eilandert's standalone-script form.

Coverage gap this closes
------------------------
The existing suite already exercises each axis *separately* but never their
intersection:
  * test_proxy_flush.py drives `proxy_buffering off`, but deliberately keeps
    bodies under ZSTD_CStreamInSize (~128 KiB, ~60 KiB chunks) "so the bug
    isn't masked by libzstd's natural spill" — it does NOT sweep the 131072
    boundary.
  * test_h1/h2_truncation.py sweep the 131072 boundary, but over a *buffered*
    transport, not `proxy_buffering off`.
The uncovered intersection is: `proxy_buffering off` × {tiny zero-output
sizes, the 131071/131072/131073 boundary, multi-MB}. This test fills it.

Two failure modes it discriminates
----------------------------------
1. Tiny bodies (1, 1024 B): libzstd buffers sub-block input internally; the
   forced flush that `proxy_buffering off` induces makes a compress call that
   yields zero output. A filter that forwards that zero-size buffer trips
   nginx's `zero size buf in writer` and aborts the request.
2. Boundary / multi-MB bodies: the compressed stream genuinely crosses output
   buffers; a mishandled flush/last op truncates the frame mid-stream.

Rig discipline (from the upstream bug-B hunt): decode and byte-compare the
WHOLE body and assert exact length — never trust a 200 or a non-empty body
alone. Incompressible (os.urandom) fixtures keep the compressed stream ~1:1 so
it genuinely crosses output buffers; distinct bytes per size make a wrong-body
response detectable.

Our V2 per-call-op body filter is believed to already handle this correctly
(op precedence last→ZSTD_e_end before flush→ZSTD_e_flush, flush cleared on
drain, empty-buffer emit guard). This test is the standing proof of that.
"""

from __future__ import annotations

import http.server
import os
import socketserver
import subprocess
import threading
import time
from typing import Iterator

import pytest
import requests

from conftest import render_template, start_nginx, stop_nginx

# Distinct from the other fixture servers: test_filter_eligibility (9000),
# test_websocket (9001), test_http2_proxy_flush (9002), test_proxy_flush
# (9003), test_infinite_loop (9004), test_auto_window (9005),
# test_workspace_rss (9006), test_recycled_buffer_flags (9007).
FIXTURE_PORT = 9008
NGINX_URL = "http://127.0.0.1:8080"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

# Sizes straddle ZSTD_CStreamInSize (~131072 = 128 KiB), the historical
# boundary for this module's truncation bug class. 1 and 1024 sit below one
# zstd block (input buffered internally → the forced-flush zero-output path);
# the rest force multi-buffer streaming.
SIZES = [1, 1024, 131071, 131072, 131073, 200000, 1048576]

# The boundary bug was intermittent; re-request each size a few times.
REPEAT = 4

# Built once by the backend fixture: distinct incompressible bytes per size.
# The handler serves these and the test compares against the same object, so
# "served bytes == compared bytes" holds without cross-run determinism.
FIXTURES: dict[int, bytes] = {}


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serve any path ending in `/<n>` as exactly FIXTURES[n] bytes, **chunked,
    no Content-Length**, written in small records so the response is a genuine
    streamed chunked body — the production transport shape."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence the access log
        pass

    def do_GET(self):
        name = self.path.rsplit("/", 1)[-1].split("?")[0]
        try:
            data = FIXTURES[int(name)]
        except (ValueError, KeyError):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        mv = memoryview(data)
        step = 16384
        for i in range(0, len(mv), step):
            chunk = bytes(mv[i:i + step])
            self.wfile.write(b"%X\r\n" % len(chunk) + chunk + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")


class _ThreadingHTTPServer(socketserver.ThreadingMixIn,
                           socketserver.TCPServer):
    # A single-threaded server is stall-killed by the size matrix and yields
    # mass false failures — must be threaded.
    allow_reuse_address = True
    daemon_threads = True


EXTRA_LOCATIONS = f"""
    # The exact production trigger: unbuffered proxying of a chunked,
    # no-Content-Length upstream through the zstd body filter.
    location /unbuffered/ {{
        proxy_pass http://127.0.0.1:{FIXTURE_PORT}/;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
    }}
"""


@pytest.fixture(scope="module")
def unbuffered_backend() -> Iterator[None]:
    """Module-scoped threaded HTTP backend serving the size matrix chunked."""
    for n in SIZES:
        FIXTURES[n] = os.urandom(n)
    server = _ThreadingHTTPServer(("127.0.0.1", FIXTURE_PORT), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Self-check the backend BEFORE trusting any end-to-end result.
    for n in (1, 131072, 1048576):
        raw = requests.get(
            f"http://127.0.0.1:{FIXTURE_PORT}/b/{n}", timeout=10
        ).content
        assert len(raw) == n, (
            f"backend self-check failed: /b/{n} returned {len(raw)} bytes"
        )
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture(scope="module")
def nginx_unbuffered(unbuffered_backend) -> Iterator[str]:
    """Module-scoped nginx with the proxy_buffering-off location rendered in."""
    stop_nginx()
    render_template(extra_locations=EXTRA_LOCATIONS.strip())
    start_nginx()
    try:
        yield NGINX_URL
    finally:
        stop_nginx()


def _fetch_raw(url: str, timeout: float = 30.0) -> tuple[requests.Response, bytes]:
    """GET with `Accept-Encoding: zstd`, returning the raw on-the-wire bytes
    (urllib3's auto-zstd decoder bypassed via decode_content=False) so a
    truncated/aborted frame is visible as missing magic or a short body."""
    r = requests.get(
        url, headers={"Accept-Encoding": "zstd"}, timeout=timeout, stream=True
    )
    body = bytearray()
    try:
        for chunk in r.raw.stream(amt=65536, decode_content=False):
            if chunk:
                body.extend(chunk)
    finally:
        r.close()
    return r, bytes(body)


@pytest.mark.parametrize("size", SIZES, ids=[str(s) for s in SIZES])
def test_proxy_unbuffered_truncation(nginx_unbuffered: str, size: int) -> None:
    expected = FIXTURES[size]
    for attempt in range(1, REPEAT + 1):
        tag = f"size={size} attempt={attempt}/{REPEAT}"
        r, blob = _fetch_raw(f"{nginx_unbuffered}/unbuffered/b/{size}")

        assert r.headers.get("Content-Encoding", "").lower() == "zstd", (
            f"[{tag}] expected Content-Encoding=zstd, got "
            f"{r.headers.get('Content-Encoding')!r}"
        )
        assert blob[:4] == ZSTD_MAGIC, (
            f"[{tag}] missing zstd magic; first 16B hex={blob[:16].hex()} "
            f"(truncated / aborted response — the 'zero size buf in writer' "
            f"signature for tiny bodies)"
        )

        dec = subprocess.run(
            ["zstd", "-d", "-q", "-c"], input=blob, capture_output=True
        )
        assert dec.returncode == 0, (
            f"[{tag}] zstd -d failed (premature end / corrupt frame): "
            f"{dec.stderr.decode('utf-8', 'replace').strip()}"
        )
        assert len(dec.stdout) == size, (
            f"[{tag}] decoded {len(dec.stdout)} bytes, expected {size} "
            f"(TRUNCATION)"
        )
        assert dec.stdout == expected, (
            f"[{tag}] decoded body differs from origin at equal length "
            f"(CORRUPTION)"
        )
