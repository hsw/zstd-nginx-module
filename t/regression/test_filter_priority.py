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
from pathlib import Path

import pytest

from conftest import BASE_URL, http_request, render_template, start_nginx, stop_nginx


def _has_brotli() -> bool:
    """Detect whether this nginx supports brotli. Two valid paths:
      * static build (ubuntu-24.04-brotli) — `nginx -V` lists ngx_brotli
      * dynamic build (ubuntu-24.04-dynamic-brotli) — brotli .so present
        in /etc/nginx/modules and loaded via load_module at runtime
    """
    out = subprocess.run(
        ["nginx", "-V"], capture_output=True, text=True, check=False
    )
    if "ngx_brotli" in (out.stdout + out.stderr):
        return True
    return Path("/etc/nginx/modules/ngx_http_brotli_filter_module.so").exists()


def _brotli_load_modules() -> str:
    """Return the load_module lines needed when running the brotli combo
    tests. Empty string on the static build (everything is linked into
    the nginx binary); on the dynamic-brotli build, load brotli + zstd
    filter + static modules so the test fixture renders a config that
    nginx -t accepts.

    Module load order in the directive does NOT control ngx_modules[]
    ordering — that's owned by `ngx_module_order` declared in each
    module's `config` file. We still write them in brotli-then-zstd
    order to keep the rendered conf self-documenting.
    """
    nv = subprocess.run(
        ["nginx", "-V"], capture_output=True, text=True, check=False,
    )
    if "ngx_brotli" in (nv.stdout + nv.stderr):
        return ""  # static build, modules linked in
    parts = []
    for name in (
        "ngx_http_brotli_filter_module.so",
        "ngx_http_brotli_static_module.so",
        "ngx_http_zstd_filter_module.so",
        "ngx_http_zstd_static_module.so",
    ):
        if Path(f"/etc/nginx/modules/{name}").exists():
            parts.append(f"load_module modules/{name};")
    return "\n    ".join(parts)


pytestmark = pytest.mark.skipif(
    not _has_brotli(), reason="nginx without ngx_brotli (neither linked nor loadable)"
)


@pytest.fixture(scope="module")
def brotli_nginx():
    """Render config with brotli enabled inline. Loads brotli+zstd
    modules dynamically on the dynamic-brotli image; no load_module
    lines on the static-brotli image (modules linked into the binary)."""
    stop_nginx()
    render_template(
        extra_directives="brotli on; brotli_min_length 0; brotli_types *;",
        load_modules=_brotli_load_modules(),
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
