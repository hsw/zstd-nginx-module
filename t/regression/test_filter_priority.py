"""Pytest port of t/regression/filter-priority.sh.

Task 8 (static-build filter ordering). Only meaningful on the brotli image
(ubuntu-24.04-brotli) where ngx_brotli is statically linked into the same
nginx binary as the zstd filter module. The `filter/config` sed re-orders
HTTP_FILTER_MODULES so brotli runs FIRST, then zstd, then gzip — meaning
zstd wins over gzip but loses to brotli when both are accepted by the
client.

Coverage (3 Accept-Encoding cases against /text):
  1. "gzip, br, zstd"       → Content-Encoding: zstd   (zstd outermost wins)
  2. "gzip, br"             → Content-Encoding: br     (brotli > gzip)
  3. "zstd;q=0, gzip, br"   → Content-Encoding: br     (zstd declined; br > gzip)

Module-skipped on non-brotli builds via `nginx -V | grep ngx_brotli`.
"""

from __future__ import annotations

import subprocess

import pytest

from conftest import BASE_URL, http_request, render_template, start_nginx, stop_nginx


def _has_brotli() -> bool:
    """Mirror the bash gate: `nginx -V | grep ngx_brotli`. Only the brotli
    image satisfies; everything else skips the whole module."""
    out = subprocess.run(
        ["nginx", "-V"], capture_output=True, text=True, check=False
    )
    return "ngx_brotli" in (out.stdout + out.stderr)


pytestmark = pytest.mark.skipif(
    not _has_brotli(), reason="nginx not built with ngx_brotli"
)


@pytest.fixture(scope="module")
def brotli_nginx():
    """Render config with brotli enabled inline (static build: no
    load_module line; brotli must be turned on via directives)."""
    stop_nginx()
    render_template(
        extra_directives="brotli on; brotli_min_length 0; brotli_types *;",
        load_modules="",  # static brotli build links everything in — no .so loads.
    )
    start_nginx()
    try:
        yield BASE_URL
    finally:
        stop_nginx()


# (label, accept_encoding, expected_content_encoding)
CASES = [
    # zstd present in offer set → zstd wins over gzip and br
    ("zstd-wins",              "gzip, br, zstd",       "zstd"),
    # zstd absent, brotli present → br wins over gzip
    ("br-beats-gzip",          "gzip, br",             "br"),
    # zstd explicitly declined (q=0), brotli + gzip offered → br wins
    ("zstd-declined-br-wins",  "zstd;q=0, gzip, br",   "br"),
]


@pytest.mark.parametrize(
    "label,accept_encoding,expected_ce",
    CASES,
    ids=[c[0] for c in CASES],
)
def test_filter_priority(brotli_nginx, label, accept_encoding, expected_ce):
    r, _ = http_request(
        brotli_nginx, "/text", method="HEAD", accept_encoding=accept_encoding
    )
    ce = r.headers.get("Content-Encoding", "")
    assert ce == expected_ce, (
        f"[{label}] AE={accept_encoding!r}: "
        f"expected Content-Encoding={expected_ce!r}, got {ce!r}; "
        f"all headers: {dict(r.headers)}"
    )
