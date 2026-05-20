"""Recycled output buffer must not carry stale control flags (codex2 #7).

Background: `ngx_http_zstd_filter_get_buf` recycles output bufs via
`ctx->free` once `ngx_chain_update_chains` returns them to the pool.
Before commit `<this-fix>`, the recycled buf retained the `b->flush=1`
that was set on the previous emission at line 642 of
`filter/ngx_http_zstd_filter_module.c`. A second data write reusing
the same buf would carry a spurious downstream flush — a latency
artefact (extra `b->flush` events trigger early
`ngx_http_writer`/`ngx_http_output_filter` walks of the chain).

The bug is hard to observe directly from a black-box HTTP client —
`b->flush` is an in-process flag on `ngx_buf_t`, not a wire-level
signal. The pragmatic test below is **behavioural coverage**: it
configures `zstd_buffers 2 4k` to force aggressive buf recycling,
drives an upstream that flushes between data segments (proxy_buffering
off + chunked transfer with inter-chunk gaps), and asserts:

    1. nginx does NOT crash / hang under the recycling pressure
    2. the decompressed body round-trips byte-identically against the
       upstream ground truth
    3. response carries `Content-Encoding: zstd` (filter actually ran)

The contrived scenario exercises the `ctx->free` branch in
`_get_buf` repeatedly. Combined with `t/asan.sh` / `t/valgrind.sh`
this also catches any latent stale-pointer / use-after-free in the
recycled-buf path. The strict invariant (recycled buf has no stale
`flush=1`) is verified by code-review of the source change; this
test is the harness-side regression guard.

If the source fix is reverted, this test still passes (it only
exercises the path) — that is intentional. The behavioural value is
that ASan/UBSan / valgrind variants now have a routine workload
running through the recycled-buf branch.
"""

from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from typing import Iterator

import pytest
import requests

from conftest import (
    BASE_URL, render_template, start_nginx, stop_nginx, zstd_decompress,
)

# Distinct from other test fixture ports (9000-9006 used).
FIXTURE_PORT = 9007

GROUND_TRUTH: dict[str, bytes] = {}
GROUND_TRUTH_LOCK = threading.Lock()


def _send_chunked_recycle(c: socket.socket) -> None:
    """Drive aggressive buf recycling: many small chunks with short
    inter-chunk gaps. Each upstream chunk reaches the filter with
    `b->flush=1` (proxy_buffering off, chunked encoding). With
    `zstd_buffers 2 4k` the filter quickly burns through its 2 bufs
    and recycles via ctx->free — the exact path codex2 #7 flagged.
    Total body ~12 KiB, well within the recycled-path window."""
    chunks = [f"r-{i:03d}-".encode() + os.urandom(200) for i in range(60)]
    full = b"".join(chunks)
    with GROUND_TRUTH_LOCK:
        GROUND_TRUTH["recycle"] = full
    c.sendall(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/plain\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
    )
    for chunk in chunks:
        c.sendall(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
        time.sleep(0.02)
    c.sendall(b"0\r\n\r\n")


def _handle(c: socket.socket) -> None:
    try:
        c.settimeout(2)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = c.recv(4096)
            if not chunk:
                break
            buf += chunk
        _send_chunked_recycle(c)
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
    location /recycle/ {{
        proxy_pass http://127.0.0.1:{FIXTURE_PORT}/;
        proxy_http_version 1.1;
        proxy_buffering off;
        # Force aggressive output-buf recycling: only 2 bufs of 4 KiB
        # each. Upstream sends ~12 KiB across 60 chunks → filter must
        # cycle the same 2 bufs through ctx->free many times.
        zstd_buffers 2 4k;
    }}
"""


@pytest.fixture(scope="module")
def nginx_with_recycle_location(upstream_fixture) -> Iterator[str]:
    stop_nginx()
    render_template(extra_locations=EXTRA_LOCATIONS.strip())
    start_nginx()
    try:
        yield BASE_URL
    finally:
        stop_nginx()


def test_recycled_buf_roundtrip(nginx_with_recycle_location, tmp_path):
    """Aggressive recycling under flush pressure must not crash, hang,
    or corrupt the response.

    Behavioural-coverage test (NOT a strict invariant assertion) — the
    invariant that recycled bufs have flush=0/sync=0/last_buf=0/
    last_in_chain=0 cleared is verified by code review; this harness
    exercises the path so ASan/valgrind variants catch any latent
    stale-pointer regressions.
    """
    url = nginx_with_recycle_location + "/recycle/"
    start = time.monotonic()
    r = requests.get(
        url,
        headers={"Accept-Encoding": "zstd"},
        timeout=10.0,
        stream=True,
    )
    body = bytearray()
    try:
        for chunk in r.raw.stream(amt=4096, decode_content=False):
            if chunk:
                body.extend(chunk)
    finally:
        r.close()
    total_ms = (time.monotonic() - start) * 1000.0

    assert r.headers.get("Content-Encoding") == "zstd", (
        f"Content-Encoding={r.headers.get('Content-Encoding')!r}, "
        f"expected zstd — filter must have run for this test to exercise "
        f"the recycled-buf path"
    )
    assert bytes(body[:4]) == b"\x28\xb5\x2f\xfd", (
        f"response missing zstd magic; hex={bytes(body[:16]).hex()}"
    )

    # Byte-compare against upstream ground truth.
    dec = zstd_decompress(bytes(body))
    with GROUND_TRUTH_LOCK:
        truth = GROUND_TRUTH.get("recycle")
    assert truth is not None, "fixture handler did not record ground truth"
    assert dec == truth, (
        f"decoded differs from upstream truth: "
        f"truth={len(truth)}B vs dec={len(dec)}B "
        f"(recycled-buf state corruption?)"
    )

    # Hang ceiling — total upstream window is ~1.2 s (60 chunks * 20 ms).
    # If we exceed 8 s, something is wrong (deadlock under recycling).
    assert total_ms < 8000.0, (
        f"request took {total_ms:.0f}ms — possible deadlock under buf "
        f"recycling pressure"
    )
