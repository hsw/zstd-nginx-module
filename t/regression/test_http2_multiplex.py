"""HTTP/2 multiplex stress on the zstd body filter.

V2 roadmap item #4 (docs/TODO.md): single-stream test_h2_truncation
doesn't surface the multiplex condition the upstream PR #49 description
referenced. PR #49's bug fired when several HTTP/2 streams concurrently
ran through the same worker's compress filter — worker-level state
leaked across streams.

This file opens N concurrent HTTP/2 GET streams on ONE connection (httpx
multiplexes by default — same client, same HTTPCore connection pool ⇒
streams share the underlying TLS session) and asserts that every stream
gets back a valid zstd frame that decodes byte-identical to its origin
file.

Test matrix:
  * 10 streams × 4 boundary sizes (200000 / 131071 / 131072 / 131073) =
    40 requests, 10-way multiplex per fixture invocation.
  * Each stream chooses a body size round-robin; sizes are deliberately
    around the 128 KiB / ZSTD_CStreamInSize boundary so PR #49's
    truncation pattern has the best chance of firing.
  * Failure modes worth catching:
    1. Truncated stream (decode fails OR decoded length != origin)
    2. Cross-stream contamination (stream A's body bytes appear in B)
    3. Worker crash mid-multiplex (httpx surfaces RemoteProtocolError /
       stream RST)

httpx 0.28's AsyncClient is the canonical way to drive multiple streams
concurrently — `await asyncio.gather(*[client.get(url) for ...])`
on one client instance pools the connection and multiplexes.

Coverage scope: byte-equality + framing integrity per stream. Does NOT
assert specific HTTP/2 frame counts or stream IDs — that would require
a low-level h2 lib (see V2 #3 for the RFC 8441 approach which uses h2
directly).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
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

FIXTURE_DIR = Path("/var/fixtures/random")
SIZES = [200000, 131071, 131072, 131073]

# Same TLS-listen pattern as test_h2_truncation / test_http2_proxy_flush:
# httpx 0.28 needs TLS+ALPN to negotiate HTTP/2 — no h2c support.
H2_BASE_URL = "https://127.0.0.1:8443"
EXTRA_SERVER_TLS = """
        listen 8443 ssl;
        ssl_certificate /etc/nginx/test-cert.pem;
        ssl_certificate_key /etc/nginx/test-key.pem;
"""


def _has_http2() -> bool:
    import subprocess
    out = subprocess.run(
        ["nginx", "-V"], capture_output=True, text=True, check=False,
    )
    return "--with-http_v2_module" in (out.stdout + out.stderr)


pytestmark = pytest.mark.skipif(
    not _has_http2(), reason="nginx built without --with-http_v2_module",
)


@pytest.fixture(scope="module")
def nginx_h2_mux() -> Iterator[str]:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for n in SIZES:
        p = FIXTURE_DIR / str(n)
        if not p.exists() or p.stat().st_size != n:
            p.write_bytes(os.urandom(n))

    load_modules = (
        "load_module modules/ngx_http_zstd_filter_module.so;"
        if _nginx_has_compat() else ""
    )
    stop_nginx()
    render_template(
        extra_directives="http2 on;",
        extra_server=EXTRA_SERVER_TLS,
        load_modules=load_modules,
    )
    start_nginx()
    try:
        yield H2_BASE_URL
    finally:
        stop_nginx()


async def _fetch_one(
    client: httpx.AsyncClient, base: str, size: int, idx: int
) -> tuple[int, int, bytes]:
    """Single stream: GET /random/<size>, return (idx, size, raw_body).
    Caller verifies framing and decompression."""
    url = f"{base}/random/{size}"
    async with client.stream(
        "GET", url, headers={"Accept-Encoding": "zstd"},
    ) as r:
        assert r.http_version == "HTTP/2", (
            f"[stream {idx}, size {size}] http_version={r.http_version!r} "
            f"— httpx did not negotiate HTTP/2"
        )
        assert r.status_code == 200, (
            f"[stream {idx}, size {size}] status={r.status_code}"
        )
        assert r.headers.get("content-encoding") == "zstd", (
            f"[stream {idx}, size {size}] Content-Encoding="
            f"{r.headers.get('content-encoding')!r}"
        )
        body = b"".join([chunk async for chunk in r.aiter_raw()])
    return idx, size, body


@pytest.mark.parametrize("stream_count", [10, 20])
def test_h2_multiplex_roundtrip(nginx_h2_mux, stream_count):
    """Open `stream_count` concurrent streams on ONE httpx.AsyncClient
    (httpx multiplexes via the shared HTTPCore connection pool). Each
    stream picks a size round-robin from SIZES. After all streams
    complete, byte-compare each decoded body against its origin file.

    Cross-stream contamination would manifest as decode-mismatch on at
    least one stream. PR #49-style truncation would manifest as decode
    failure OR length mismatch.
    """
    async def driver():
        # http2=True + http1=False forces httpx to ONLY do h2 — if any
        # stream falls back to h1.1, the assertion in _fetch_one catches it.
        # verify=False because the test cert is self-signed.
        # limits: ensure max_connections=1 so all streams MUST multiplex
        # onto one TCP connection (otherwise httpx might pool-spawn 10
        # separate connections, defeating the multiplex test).
        limits = httpx.Limits(
            max_connections=1, max_keepalive_connections=1
        )
        async with httpx.AsyncClient(
            http2=True, http1=False, verify=False,
            timeout=30, limits=limits,
        ) as client:
            tasks = [
                _fetch_one(client, nginx_h2_mux, SIZES[i % len(SIZES)], i)
                for i in range(stream_count)
            ]
            results = await asyncio.gather(*tasks)
        return results

    results = asyncio.run(driver())

    failures = []
    for idx, size, body in results:
        if body[:4] != b"\x28\xb5\x2f\xfd":
            failures.append(
                f"stream {idx} size {size}: missing zstd magic "
                f"(first 16B: {body[:16].hex()})"
            )
            continue
        try:
            decoded = zstd_decompress(body)
        except Exception as e:
            failures.append(
                f"stream {idx} size {size}: zstd decode failed: {e}"
            )
            continue
        expected = (FIXTURE_DIR / str(size)).read_bytes()
        if decoded != expected:
            failures.append(
                f"stream {idx} size {size}: decoded differs "
                f"(orig={len(expected)} dec={len(decoded)})"
            )

    assert not failures, (
        f"{len(failures)}/{stream_count} streams failed:\n"
        + "\n".join(f"  - {f}" for f in failures[:10])
    )


def test_h2_multiplex_mixed_sizes_round_trip(nginx_h2_mux):
    """Variant: 12 streams, INTENTIONALLY using all 4 sizes 3× each so the
    multiplex layer sees concurrent variation in body length. PR #49's
    truncation was size-boundary-sensitive — mixing sizes increases the
    chance of catching the bug if it ever returns."""
    async def driver():
        limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
        async with httpx.AsyncClient(
            http2=True, http1=False, verify=False,
            timeout=30, limits=limits,
        ) as client:
            tasks = []
            for repeat in range(3):
                for s in SIZES:
                    idx = repeat * len(SIZES) + SIZES.index(s)
                    tasks.append(_fetch_one(client, nginx_h2_mux, s, idx))
            return await asyncio.gather(*tasks)

    results = asyncio.run(driver())
    failures = []
    for idx, size, body in results:
        if body[:4] != b"\x28\xb5\x2f\xfd":
            failures.append(f"stream {idx} size {size}: missing magic")
            continue
        decoded = zstd_decompress(body)
        expected = (FIXTURE_DIR / str(size)).read_bytes()
        if decoded != expected:
            failures.append(
                f"stream {idx} size {size}: decoded differs "
                f"(orig={len(expected)} dec={len(decoded)})"
            )
    assert not failures, "\n".join(failures[:10])
