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
    """Render config with brotli + gzip enabled inline (zstd is on via the
    template default). Loads brotli+zstd modules dynamically on the
    dynamic-brotli image; no load_module lines on the static-brotli
    image (modules linked into the binary). gzip is needed so the
    production-traffic AE matrix can exercise gzip-only paths."""
    stop_nginx()
    render_template(
        extra_directives=(
            "brotli on; brotli_min_length 0; brotli_types *;"
            " gzip on; gzip_min_length 0; gzip_types *;"
        ),
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


# Top-10 real-world Accept-Encoding strings observed in L1 webfront traffic
# over a 24h window (~20B requests). Numbers in the label column are the
# request-share rank. The expected outcome column is the encoding the
# filter chain (zstd > br > gzip) must pick — derived from the operator-
# declared module priority, NOT from header order or q-values
# (q-values only matter for the explicit-decline shape, covered above).
#
# Empty AE is sent as an empty header value; an absent header in
# production has identical semantics for our filters (both decline
# without a matching token). We forward this as Accept-Encoding: ""
# rather than dropping the header to keep the test code uniform.
PRODUCTION_CASES = [
    # rank 1 — 30.9%: modern desktop browsers
    ("prod-01-all-four",       "gzip, deflate, br, zstd",       "zstd"),
    # rank 2 — 28.9%: pre-zstd shape; zstd absent, br wins over gzip
    ("prod-02-br-gzip",        "br, gzip",                      "br"),
    # rank 3 — 10.7%: same shape without zstd
    ("prod-03-gzip-defl-br",   "gzip, deflate, br",             "br"),
    # rank 4 — 9.2%: gzip-only client; gzip is the only supported option
    ("prod-04-gzip-only",      "gzip",                          "gzip"),
    # rank 5 — 6.9%: empty Accept-Encoding (or absent header); no
    # compression — identity response
    ("prod-05-empty",          "",                              ""),
    # rank 6 — 5.7%: zstd absent, br wins over gzip
    ("prod-06-gzip-br",        "gzip, br",                      "br"),
    # rank 7 — 3.7%: br,gzip with no whitespace after comma — RFC 9110
    # OWS tolerance asserted (parser must accept zero OWS bytes between
    # token and OWS-only separator); zstd absent, br wins
    ("prod-07-br-gzip-no-ows", "br,gzip",                       "br"),
    # rank 8 — 1.0%: gzip with deflate (deflate not supported in nginx;
    # only gzip remains as a server option)
    ("prod-08-gzip-defl",      "gzip, deflate",                 "gzip"),
    # rank 9 — 0.94%: x-gzip is the RFC 9110 synonym for gzip; nginx
    # gzip filter accepts both. Test asserts the synonym is honored;
    # outcome stays gzip
    ("prod-09-gzip-xgzip",     "gzip, x-gzip, deflate",         "gzip"),
    # rank 10 — 0.78%: sdch is deprecated and our parsers ignore it;
    # zstd present → zstd wins
    ("prod-10-all-plus-sdch",  "gzip, deflate, br, zstd, sdch", "zstd"),
]


@pytest.mark.parametrize(
    "label,accept_encoding,expected_ce",
    PRODUCTION_CASES,
    ids=[c[0] for c in PRODUCTION_CASES],
)
def test_filter_priority_production_traffic(
    brotli_nginx, label, accept_encoding, expected_ce,
):
    """Regression coverage for the top-10 Accept-Encoding shapes seen in
    24h of L1 webfront traffic (~20B requests). Asserts the filter chain
    picks the operator-preferred encoding under each real-world shape.

    Notable shapes:
      * `br,gzip` with NO whitespace after the comma (3.7% of traffic) —
        our RFC 9110 parser's OWS tolerance must accept zero-width OWS.
      * `gzip, x-gzip, deflate` (0.94%) — x-gzip is the legacy synonym
        for gzip per RFC 9110; nginx core gzip filter handles it.
      * `gzip, deflate, br, zstd, sdch` (0.78%) — sdch is dead; our
        parsers ignore unknown tokens and zstd wins.
      * Empty AE header (6.9%) — no token matches → no compression.
    """
    r, _ = http_request(
        brotli_nginx, "/text", method="HEAD", accept_encoding=accept_encoding
    )
    ce = r.headers.get("Content-Encoding", "")
    assert ce == expected_ce, (
        f"[{label}] AE={accept_encoding!r}: "
        f"expected Content-Encoding={expected_ce!r}, got {ce!r}; "
        f"all headers: {dict(r.headers)}"
    )
