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
    """Static-build: ngx_brotli is linked in (nginx -V shows it).
    Dynamic-build: ngx_brotli .so present in /etc/nginx/modules.
    Either is acceptable for the test."""
    out = subprocess.run(
        ["nginx", "-V"], capture_output=True, text=True, check=False
    )
    if "ngx_brotli" in (out.stdout + out.stderr):
        return True
    return Path("/etc/nginx/modules/ngx_http_brotli_filter_module.so").exists()


def _brotli_load_modules() -> str:
    """load_module lines for the dynamic-brotli image; empty for static."""
    nv = subprocess.run(
        ["nginx", "-V"], capture_output=True, text=True, check=False,
    )
    if "ngx_brotli" in (nv.stdout + nv.stderr):
        return ""
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
        load_modules=_brotli_load_modules(),
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


# Top-10 real-world Accept-Encoding strings observed in L1 webfront
# traffic over a 24h window (~20B requests). Same source as the matching
# matrix in test_filter_priority.py — but the expected outcomes differ
# because the static handler chain depends on which sidecars exist on
# disk and which static modules are compiled in. Our brotli image
# includes zstd_static + brotli_static but NOT --with-http_gzip_static_module,
# and the /both-static/ fixture only creates .zst + .br sidecars (no .gz).
# So any client offering only gzip/deflate falls through to the plain
# file (Content-Encoding header absent).
PRODUCTION_CASES_STATIC = [
    # rank 1 — 30.9%: zstd offered + sidecar exists → zstd_static
    ("prod-01-all-four",       "gzip, deflate, br, zstd",       "zstd"),
    # rank 2 — 28.9%: zstd not offered, br_static serves .br
    ("prod-02-br-gzip",        "br, gzip",                      "br"),
    # rank 3 — 10.7%: same shape, br_static serves .br
    ("prod-03-gzip-defl-br",   "gzip, deflate, br",             "br"),
    # rank 4 — 9.2%: gzip-only client. No .gz sidecar + no
    # gzip_static module → fall through to plain
    ("prod-04-gzip-only",      "gzip",                          None),
    # rank 5 — 6.9%: empty AE → no static module activates → plain
    ("prod-05-empty",          "",                              None),
    # rank 6 — 5.7%: zstd not offered, br_static wins
    ("prod-06-gzip-br",        "gzip, br",                      "br"),
    # rank 7 — 3.7%: zero-OWS comma between br and gzip; br_static wins
    ("prod-07-br-gzip-no-ows", "br,gzip",                       "br"),
    # rank 8 — 1.0%: no sidecar for gzip/deflate → plain
    ("prod-08-gzip-defl",      "gzip, deflate",                 None),
    # rank 9 — 0.94%: x-gzip is the gzip synonym; we have no .gz
    # sidecar in this fixture → plain
    ("prod-09-gzip-xgzip",     "gzip, x-gzip, deflate",         None),
    # rank 10 — 0.78%: sdch ignored, zstd present → zstd_static
    ("prod-10-all-plus-sdch",  "gzip, deflate, br, zstd, sdch", "zstd"),
]


@pytest.mark.parametrize(
    "label,accept_encoding,expected_ce",
    PRODUCTION_CASES_STATIC,
    ids=[c[0] for c in PRODUCTION_CASES_STATIC],
)
def test_static_priority_production_traffic(
    static_priority_nginx, label, accept_encoding, expected_ce,
):
    """Regression coverage for the top-10 Accept-Encoding shapes seen
    in 24h of L1 webfront traffic (~20B requests). Asserts the static
    handler chain picks the operator-preferred sidecar under each
    real-world shape, falling through to the plain file when no static
    module can serve the client.

    expected_ce == None means "no Content-Encoding header" — sidecar
    chain declined, plain file served.

    Notable shapes covered:
      * `br,gzip` with no OWS between tokens (3.7% of traffic).
      * `gzip, x-gzip, deflate` — x-gzip ignored, falls through.
      * gzip-only client (9.2%) — no .gz sidecar in our fixture so
        falls through to plain; in a real deployment with gzip_static
        + .gz sidecars this would be Content-Encoding: gzip.
      * Empty AE header (6.9%) — no sidecar activates → plain.
    """
    r, body = http_request(
        static_priority_nginx, "/both-static/sample",
        accept_encoding=accept_encoding,
    )
    assert r.status_code == 200, f"[{label}] HTTP {r.status_code}"
    actual_ce = r.headers.get("Content-Encoding")
    if expected_ce is None:
        assert actual_ce not in ("zstd", "br"), (
            f"[{label}] AE={accept_encoding!r}: expected plain, got "
            f"Content-Encoding={actual_ce!r}"
        )
        # In our build, "not zstd not br" actually means absent (no
        # gzip_static module to set it). Double-check the body matches
        # the plain file so a transparent passthrough of e.g. a stale
        # cached encoded body would still fail.
        assert body == PLAIN_FILE.read_bytes(), (
            f"[{label}] AE={accept_encoding!r}: expected plain bytes, "
            f"got {len(body)} bytes (plain is {PLAIN_FILE.stat().st_size})"
        )
    else:
        assert actual_ce == expected_ce, (
            f"[{label}] AE={accept_encoding!r}: expected "
            f"Content-Encoding={expected_ce!r}, got {actual_ce!r}; "
            f"all headers: {dict(r.headers)}"
        )
        expected_body = (
            ZST_SIDECAR.read_bytes() if expected_ce == "zstd"
            else BR_SIDECAR.read_bytes()
        )
        assert body == expected_body, (
            f"[{label}] AE={accept_encoding!r}: served body mismatched "
            f"sidecar — possible content-handler / load-module-order bug"
        )
