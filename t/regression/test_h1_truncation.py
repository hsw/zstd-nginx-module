"""HTTP/1.1 axis for PR #49 truncation bug.

Companion to test_h2_truncation.py — the silent-truncation bug at 131072
bytes (decompressed) is NOT HTTP/2-specific. HTTP/2 framing originally
made it more visible (short DATA frame), but the root cause lives in the
body filter's FLUSH-to-done transition and fires on any path that delivers
a single chain link with last_buf=1 carrying more than ZSTD_CStreamInSize
bytes.

Coverage: serve deterministic bodies of 131071 / 131072 / 131073 / 200000
bytes over plain HTTP/1.1, decompress, byte-diff against origin. Same
output_buffers + sendfile trick as the HTTP/2 test forces single-chain
delivery so the bug isn't masked by the default 32 KiB chunking.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from conftest import (
    BASE_URL,
    _nginx_has_compat,
    render_template,
    start_nginx,
    stop_nginx,
    zstd_decompress,
)

FIXTURE_DIR = Path("/var/fixtures/random")
SIZES = [131071, 131072, 131073, 200000]


@pytest.fixture(scope="module")
def h1_nginx():
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for n in SIZES:
        p = FIXTURE_DIR / str(n)
        if not p.exists() or p.stat().st_size != n:
            p.write_bytes(os.urandom(n))

    load_modules = (
        "load_module modules/ngx_http_zstd_filter_module.so;"
        if _nginx_has_compat() else ""
    )
    # See test_h2_truncation for why these three directives are required to
    # repro #49 — TL;DR: default output_buffers (2 32k) chunks the file and
    # masks the bug; we need one chain link with last_buf=1 > 131072 bytes.
    extra_directives = "output_buffers 1 1m; sendfile on; aio off;"
    stop_nginx()
    render_template(
        extra_directives=extra_directives, load_modules=load_modules,
    )
    start_nginx()
    try:
        yield BASE_URL
    finally:
        stop_nginx()


@pytest.mark.parametrize("size", SIZES, ids=[str(n) for n in SIZES])
def test_h1_roundtrip(h1_nginx, size):
    """curl -> raw zstd bytes -> decompress -> byte-diff against origin.
    Uses curl (not requests) to avoid client-side auto-decode pipelines."""
    url = f"{h1_nginx}/random/{size}"
    r = subprocess.run(
        ["curl", "-sS", "--max-time", "10",
         "-H", "Accept-Encoding: zstd", url],
        capture_output=True, check=True,
    )
    body = r.stdout
    assert body[:4] == b"\x28\xb5\x2f\xfd", (
        f"size={size}: missing zstd magic; first 16B hex={body[:16].hex()}"
    )
    decoded = zstd_decompress(body)
    expected = (FIXTURE_DIR / str(size)).read_bytes()
    assert decoded == expected, (
        f"size={size}: decoded differs from origin "
        f"(orig={len(expected)} dec={len(decoded)})"
    )
