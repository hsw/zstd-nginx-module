"""C12-2 regression: a known Content-Length: 0 response must NOT be compressed.

This differs from test_range_304_empty.py::test_empty_200_no_crash precisely by
`zstd_min_length 0`. That test serves an empty 200 under the *default*
min_length=20 — which the header filter already declines via the
`content_length_n < min_length` gate — so it tolerates either outcome and can
never catch C12-2. The C12-2 bite is only under `zstd_min_length 0` + a known
zero-length body: before the fix the empty body still got `Content-Encoding:
zstd` (a pointless ~13-byte empty zstd frame) and, on the known-CL auto-window
path, a full baseline workspace allocated for zero bytes.

The fix declines compression whenever `content_length_n == 0`, regardless of
min_length — nothing is ever gained by compressing an empty body.

Out of scope (working-as-intended, gzip-parity): empty chunked / unknown-length
responses (content_length_n == -1 at header time) still commit
`Content-Encoding: zstd` and emit a valid empty zstd frame, because the encoding
decision is made before any body arrives. See test_range_304_empty.py for the
default-min_length empty path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (
    BASE_URL,
    _nginx_has_compat,
    http_request,
    render_template,
    start_nginx,
    stop_nginx,
)

FIXTURE_ROOT = Path("/var/fixtures/empty-body")
EMPTY_HTML = FIXTURE_ROOT / "empty.html"


@pytest.fixture(scope="module")
def empty_body_nginx():
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    # 0-byte file served by the core static handler -> known Content-Length: 0,
    # Content-Type: text/html (compressible per zstd_types).
    EMPTY_HTML.write_bytes(b"")

    load_modules = (
        "load_module modules/ngx_http_zstd_filter_module.so;"
        if _nginx_has_compat() else ""
    )

    extra_locations = """
        location = /empty-html {
            zstd_min_length 0;
            zstd_types text/html;
            alias /var/fixtures/empty-body/empty.html;
            default_type text/html;
        }
"""
    stop_nginx()
    render_template(extra_locations=extra_locations, load_modules=load_modules)
    start_nginx()
    try:
        yield BASE_URL
    finally:
        stop_nginx()


def test_known_content_length_zero_not_compressed(empty_body_nginx):
    """Known Content-Length: 0 + zstd_min_length 0: must NOT be compressed.

    The core static handler serves the 0-byte file with a concrete
    Content-Length: 0, so the header filter knows the body is empty before any
    bytes flow. Compressing it is pure waste (empty frame + baseline workspace),
    so the filter must decline regardless of min_length.
    """
    r, body = http_request(empty_body_nginx, "/empty-html", accept_encoding="zstd")
    assert r.status_code == 200, f"status={r.status_code}"
    assert r.headers.get("Content-Encoding") not in ("zstd",), (
        f"known Content-Length: 0 must not be zstd-encoded, got "
        f"Content-Encoding={r.headers.get('Content-Encoding')!r}"
    )
    assert body == b"", f"empty body must stay empty, got {len(body)} bytes"
