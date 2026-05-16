"""Pytest port of t/regression/h2-truncation.sh.

Regression for PR #49 / Task 3: the body filter used to truncate trailing
bytes when accumulated compressed size hit 128 KiB. HTTP/2 framing exposed
the truncation as a short DATA frame; HTTP/1.1 chunked encoding masked it.

Coverage: serve deterministic bodies of 200000 / 131071 / 131072 / 131073
bytes over HTTP/2 cleartext (h2c), decompress, byte-diff against origin.

Uses curl subprocess for HTTP/2 — python3-httpx isn't in apt, and pulling
it via pip would add complexity for one use case.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import zstandard

from conftest import (
    zstd_decompress,
    BASE_URL,
    _nginx_has_compat,
    render_template,
    start_nginx,
    stop_nginx,
)

FIXTURE_DIR = Path("/var/fixtures/random")
SIZES = [200000, 131071, 131072, 131073]


def _has_http2() -> bool:
    """Skip the whole module if nginx wasn't built with --with-http_v2_module
    (e.g. the brotli static-link variant)."""
    out = subprocess.run(
        ["nginx", "-V"], capture_output=True, text=True, check=False
    )
    return "--with-http_v2_module" in (out.stdout + out.stderr)


pytestmark = pytest.mark.skipif(
    not _has_http2(), reason="nginx built without --with-http_v2_module"
)


@pytest.fixture(scope="module")
def h2_nginx():
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for n in SIZES:
        p = FIXTURE_DIR / str(n)
        if not p.exists() or p.stat().st_size != n:
            p.write_bytes(os.urandom(n))

    load_modules = (
        "load_module modules/ngx_http_zstd_filter_module.so;"
        if _nginx_has_compat() else ""
    )
    # http2 on; at http context (nginx mainline 1.25.1+ accepts it here).
    extra_directives = "http2 on;"
    stop_nginx()
    render_template(extra_directives=extra_directives, load_modules=load_modules)
    start_nginx()
    try:
        yield BASE_URL
    finally:
        stop_nginx()


@pytest.mark.parametrize("size", SIZES, ids=[str(n) for n in SIZES])
def test_h2_roundtrip(h2_nginx, size, tmp_path):
    """--http2-prior-knowledge: speak h2c without the upgrade handshake;
    sufficient for `http2 on;` cleartext server."""
    out = tmp_path / f"{size}.body"
    r = subprocess.run(
        [
            "curl", "-sS", "--http2-prior-knowledge",
            "-H", "Accept-Encoding: zstd",
            "--max-time", "30",
            f"{h2_nginx}/random/{size}",
            "-o", str(out),
        ],
        capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, f"curl failed: rc={r.returncode}\n{r.stderr}"

    dec = zstd_decompress(out.read_bytes())
    expected = (FIXTURE_DIR / str(size)).read_bytes()
    assert dec == expected, (
        f"size={size}: decoded body differs from origin "
        f"(orig={len(expected)} dec={len(dec)})"
    )
