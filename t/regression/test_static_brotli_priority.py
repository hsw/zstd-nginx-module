"""Static module ordering: zstd_static must win over brotli_static / gzip_static.

Covers upstream Issue #40 (mklooss). When a request hits a location that has
both `zstd_static on` and `brotli_static on` and the matching `.zst` and `.br`
sidecars both exist, the response must be the .zst sidecar (zstd is the
operator-declared preferred encoding via filter chain).

Module-skipped on non-brotli builds via `nginx -V | grep ngx_brotli` (same
gate as test_filter_priority.py).

Pre-fix (no ngx_module_order in static/config + no HTTP_MODULES sed-rewrite),
nginx default content-handler chain order applies — brotli_static was added
later in static/config dispatch and wins, so .br is served instead of .zst.
Test asserts the post-fix ordering.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from conftest import BASE_URL, http_request, render_template, start_nginx, stop_nginx

FIXTURE_DIR = Path("/var/fixtures/static-priority")
PLAIN_FILE = FIXTURE_DIR / "sample"
ZST_SIDECAR = FIXTURE_DIR / "sample.zst"
BR_SIDECAR = FIXTURE_DIR / "sample.br"


def _has_brotli() -> bool:
    out = subprocess.run(
        ["nginx", "-V"], capture_output=True, text=True, check=False
    )
    return "ngx_brotli" in (out.stdout + out.stderr)


pytestmark = pytest.mark.skipif(
    not _has_brotli(), reason="nginx not built with ngx_brotli"
)


@pytest.fixture(scope="module")
def static_priority_nginx():
    """Build a fixture with both .zst and .br precompressed sidecars,
    serve from a location that enables both zstd_static and brotli_static."""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    PLAIN_FILE.write_bytes(b"S" * 8192)
    subprocess.run(
        ["zstd", "-q", "-f", str(PLAIN_FILE), "-o", str(ZST_SIDECAR)], check=True,
    )
    subprocess.run(
        ["brotli", "-q", "11", "-f", str(PLAIN_FILE), "-o", str(BR_SIDECAR)],
        check=True,
    )

    # gzip_static directive intentionally omitted — this nginx build doesn't
    # include --with-http_gzip_static_module. The test surface is zstd_static
    # vs brotli_static priority, which is sufficient for Issue #40 coverage.
    extra_directives = "brotli_static on;"
    extra_locations = """
        location /both-static/ {
            zstd off;
            zstd_static on;
            brotli off;
            brotli_static on;
            alias /var/fixtures/static-priority/;
            default_type application/octet-stream;
        }
"""
    stop_nginx()
    render_template(
        extra_directives=extra_directives,
        extra_locations=extra_locations,
        load_modules="",
    )
    start_nginx()
    try:
        yield BASE_URL
    finally:
        stop_nginx()


def test_zstd_wins_over_brotli_when_both_accepted(static_priority_nginx):
    """AE=zstd,br with both .zst and .br sidecars → must serve .zst."""
    r, body = http_request(
        static_priority_nginx, "/both-static/sample",
        accept_encoding="zstd, br",
    )
    assert r.status_code == 200
    assert r.headers.get("Content-Encoding") == "zstd", (
        f"got Content-Encoding={r.headers.get('Content-Encoding')!r}; "
        f"expected zstd (zstd_static must outrank brotli_static)"
    )
    assert body == ZST_SIDECAR.read_bytes()


def test_brotli_served_when_zstd_declined(static_priority_nginx):
    """AE=br only → falls back to .br sidecar via brotli_static."""
    r, body = http_request(
        static_priority_nginx, "/both-static/sample", accept_encoding="br",
    )
    assert r.status_code == 200
    assert r.headers.get("Content-Encoding") == "br", (
        f"got Content-Encoding={r.headers.get('Content-Encoding')!r}; "
        f"expected br (zstd declined, brotli_static next in chain)"
    )
    assert body == BR_SIDECAR.read_bytes()


def test_brotli_served_when_zstd_q_zero(static_priority_nginx):
    """AE=zstd;q=0, br → zstd explicitly declined, brotli wins."""
    r, body = http_request(
        static_priority_nginx, "/both-static/sample",
        accept_encoding="zstd;q=0, br",
    )
    assert r.status_code == 200
    assert r.headers.get("Content-Encoding") == "br", (
        f"got Content-Encoding={r.headers.get('Content-Encoding')!r}; "
        f"expected br (zstd;q=0 declined by RFC 9110 parser)"
    )


def test_plain_served_when_neither_accepted(static_priority_nginx):
    """AE=identity → no matching sidecar, falls back to plain file."""
    r, body = http_request(
        static_priority_nginx, "/both-static/sample", accept_encoding="identity",
    )
    assert r.status_code == 200
    assert r.headers.get("Content-Encoding") not in ("zstd", "br"), (
        f"got Content-Encoding={r.headers.get('Content-Encoding')!r}; "
        f"expected plain (neither zstd nor br accepted)"
    )
    assert body == PLAIN_FILE.read_bytes()
