"""Pytest port of t/regression/h2-truncation.sh.

Regression for PR #49 / Task 3: the body filter used to truncate trailing
bytes when accumulated compressed size hit 128 KiB. HTTP/2 framing exposed
the truncation as a short DATA frame; HTTP/1.1 chunked encoding masked it.

Coverage: serve deterministic bodies of 200000 / 131071 / 131072 / 131073
bytes over HTTP/2 cleartext (h2c), decompress, byte-diff against origin.

Uses httpx with http2=True (venv-pinned >=0.27). We disable httpx's
automatic content-encoding decoding (it would silently inflate the zstd
body because zstandard is installed in the venv) — read raw bytes via
`Response.iter_raw()` inside a `stream()` context so we can both verify
the on-the-wire frame format AND distinguish frame-truncation from
decoder-tolerance.
"""

from __future__ import annotations

import os
from pathlib import Path

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

# httpx 0.28 negotiates HTTP/2 only via TLS+ALPN — there's no built-in
# h2c-prior-knowledge mode. Test images install a self-signed cert at
# /etc/nginx/test-{key,cert}.pem (see Dockerfile.dynamic et al), and the
# nginx fixture adds `listen 8443 ssl;` via the __EXTRA_SERVER__ marker.
H2_BASE_URL = "https://127.0.0.1:8443"
EXTRA_SERVER_TLS = """
        listen 8443 ssl;
        ssl_certificate /etc/nginx/test-cert.pem;
        ssl_certificate_key /etc/nginx/test-key.pem;
"""


def _has_http2() -> bool:
    """Skip the whole module if nginx wasn't built with --with-http_v2_module
    (e.g. the brotli static-link variant)."""
    import subprocess
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
    # TLS listener for ALPN-negotiated HTTP/2 — see H2_BASE_URL above.
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


@pytest.mark.parametrize("size", SIZES, ids=[str(n) for n in SIZES])
def test_h2_roundtrip(h2_nginx, size):
    """HTTP/2 over TLS via httpx.Client(http2=True). Reads raw response
    bytes (no auto-decompression), asserts zstd magic + decodes via
    conftest.zstd_decompress, byte-diffs against origin file."""
    url = f"{h2_nginx}/random/{size}"
    # verify=False — self-signed test cert (cert is /etc/nginx/test-cert.pem,
    # installed by the Dockerfile). Production code should never pass
    # verify=False; this is a test-only relaxation.
    with httpx.Client(http2=True, verify=False, timeout=30) as client:
        with client.stream(
            "GET", url, headers={"Accept-Encoding": "zstd"},
        ) as r:
            assert r.http_version == "HTTP/2", (
                f"size={size}: http_version={r.http_version!r} — "
                f"httpx did not negotiate HTTP/2 (server downgrade?)"
            )
            assert r.status_code == 200, (
                f"size={size}: status={r.status_code}"
            )
            assert r.headers.get("content-encoding") == "zstd", (
                f"size={size}: Content-Encoding="
                f"{r.headers.get('content-encoding')!r}"
            )
            # iter_raw bypasses httpx's content-decoder pipeline.
            body = b"".join(r.iter_raw())

    assert body[:4] == b"\x28\xb5\x2f\xfd", (
        f"size={size}: missing zstd magic; first 16B hex={body[:16].hex()}"
    )
    decoded = zstd_decompress(body)
    expected = (FIXTURE_DIR / str(size)).read_bytes()
    assert decoded == expected, (
        f"size={size}: decoded differs from origin "
        f"(orig={len(expected)} dec={len(decoded)})"
    )
