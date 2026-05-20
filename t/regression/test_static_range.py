"""Range / If-Range / 416 coverage for the zstd_static content handler.

Mirrors gzip_static behaviour: the .zst sidecar is a static file, so once
the handler sets `r->allow_ranges = 1` before `ngx_http_send_header()`
nginx core's range filter takes over and serves 206 Partial Content,
416 Requested Range Not Satisfiable, Accept-Ranges, and If-Range exactly
like a plain static file.

The seven cases below match the contract documented in the audit plan:
  1. `bytes=0-99`           → 206 + first 100 bytes of the .zst sidecar
  2. `bytes=100-`           → 206 + tail starting at offset 100
  3. `bytes=-50`            → 206 + last 50 bytes
  4. `bytes=10-9` (invalid) → 416
  5. `bytes=<size+1000>-`   → 416 (start beyond EOF)
  6. No Range request       → Accept-Ranges: bytes header present
  7. If-Range matching etag → 206; mismatched → 200 full (no Content-Range)
"""

from __future__ import annotations

import os
import subprocess
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

FIXTURE_ROOT = Path("/var/fixtures/static-range")
STATIC_FILE = FIXTURE_ROOT / "payload"
STATIC_SIDECAR = FIXTURE_ROOT / "payload.zst"


@pytest.fixture(scope="module")
def static_range_nginx():
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    # 64 KiB of os.urandom so the .zst sidecar is incompressible and stays
    # comparable in size to the original (281-byte highly-periodic payloads
    # gave no headroom for substantive byte-range slicing).
    STATIC_FILE.write_bytes(os.urandom(65536))
    subprocess.run(
        ["zstd", "-q", "-f", str(STATIC_FILE), "-o", str(STATIC_SIDECAR)],
        check=True,
    )

    load_modules = (
        "load_module modules/ngx_http_zstd_filter_module.so;\n"
        "load_module modules/ngx_http_zstd_static_module.so;"
        if _nginx_has_compat() else ""
    )

    extra_locations = """
        location /static-range/ {
            zstd off;
            zstd_static on;
            alias /var/fixtures/static-range/;
            default_type application/octet-stream;
        }
"""
    stop_nginx()
    render_template(extra_locations=extra_locations, load_modules=load_modules)
    start_nginx()
    try:
        yield BASE_URL
    finally:
        stop_nginx()


# ---------- case 1: prefix range ----------

def test_range_first_100_bytes(static_range_nginx):
    zst = STATIC_SIDECAR.read_bytes()
    r, body = http_request(
        static_range_nginx, "/static-range/payload",
        accept_encoding="zstd",
        headers={"Range": "bytes=0-99"},
    )
    assert r.status_code == 206, f"status={r.status_code}, want 206"
    assert r.headers.get("Content-Encoding") == "zstd"
    assert body == zst[:100], f"body mismatch: got {len(body)} bytes, want 100"
    cr = r.headers.get("Content-Range")
    assert cr == f"bytes 0-99/{len(zst)}", f"Content-Range={cr!r}"
    assert int(r.headers.get("Content-Length", "-1")) == len(body)


# ---------- case 2: open-ended range ----------

def test_range_tail_from_offset(static_range_nginx):
    zst = STATIC_SIDECAR.read_bytes()
    r, body = http_request(
        static_range_nginx, "/static-range/payload",
        accept_encoding="zstd",
        headers={"Range": "bytes=100-"},
    )
    assert r.status_code == 206, f"status={r.status_code}, want 206"
    assert r.headers.get("Content-Encoding") == "zstd"
    assert body == zst[100:], (
        f"body mismatch: got {len(body)} bytes, want {len(zst) - 100}"
    )
    cr = r.headers.get("Content-Range")
    assert cr == f"bytes 100-{len(zst) - 1}/{len(zst)}", f"Content-Range={cr!r}"
    assert int(r.headers.get("Content-Length", "-1")) == len(body)


# ---------- case 3: suffix range ----------

def test_range_suffix_last_50(static_range_nginx):
    zst = STATIC_SIDECAR.read_bytes()
    r, body = http_request(
        static_range_nginx, "/static-range/payload",
        accept_encoding="zstd",
        headers={"Range": "bytes=-50"},
    )
    assert r.status_code == 206, f"status={r.status_code}, want 206"
    assert r.headers.get("Content-Encoding") == "zstd"
    assert body == zst[-50:], f"body mismatch: got {len(body)} bytes, want 50"
    cr = r.headers.get("Content-Range")
    assert cr == f"bytes {len(zst) - 50}-{len(zst) - 1}/{len(zst)}", (
        f"Content-Range={cr!r}"
    )
    assert int(r.headers.get("Content-Length", "-1")) == len(body)


# ---------- case 4: invalid order range -> 416 ----------

def test_range_invalid_order_416(static_range_nginx):
    """bytes=10-9: start > end → 416 Requested Range Not Satisfiable."""
    r, _ = http_request(
        static_range_nginx, "/static-range/payload",
        accept_encoding="zstd",
        headers={"Range": "bytes=10-9"},
    )
    assert r.status_code == 416, f"status={r.status_code}, want 416"


# ---------- case 5: range beyond EOF -> 416 ----------

def test_range_beyond_eof_416(static_range_nginx):
    """Start offset beyond file size → 416. Ceiling is self-adapting:
    `<sidecar-size> + 1000` is always past EOF regardless of how libzstd
    happens to compress the random payload."""
    zst_size = STATIC_SIDECAR.stat().st_size
    start = zst_size + 1000
    r, _ = http_request(
        static_range_nginx, "/static-range/payload",
        accept_encoding="zstd",
        headers={"Range": f"bytes={start}-"},
    )
    assert r.status_code == 416, f"status={r.status_code}, want 416"


# ---------- case 6: Accept-Ranges advertised on plain GET ----------

def test_accept_ranges_header_advertised(static_range_nginx):
    """Non-Range GET serving .zst must include `Accept-Ranges: bytes` so
    clients know they can issue range requests."""
    r, body = http_request(
        static_range_nginx, "/static-range/payload", accept_encoding="zstd",
    )
    assert r.status_code == 200, f"status={r.status_code}"
    assert r.headers.get("Content-Encoding") == "zstd"
    assert r.headers.get("Accept-Ranges") == "bytes", (
        f"Accept-Ranges={r.headers.get('Accept-Ranges')!r}, want 'bytes'"
    )
    assert body == STATIC_SIDECAR.read_bytes()


# ---------- case 7: If-Range matching vs mismatched ----------

def test_if_range_matching_etag_206(static_range_nginx):
    """If-Range with the current ETag must succeed → 206 partial content."""
    warm, _ = http_request(
        static_range_nginx, "/static-range/payload", accept_encoding="zstd",
    )
    etag = warm.headers.get("ETag")
    assert etag, "expected ETag on sidecar response"

    r, body = http_request(
        static_range_nginx, "/static-range/payload",
        accept_encoding="zstd",
        headers={"Range": "bytes=0-99", "If-Range": etag},
    )
    assert r.status_code == 206, (
        f"status={r.status_code}, want 206 (If-Range matched)"
    )
    assert body == STATIC_SIDECAR.read_bytes()[:100]
    assert int(r.headers.get("Content-Length", "-1")) == len(body)


def test_if_range_mismatched_etag_200_full(static_range_nginx):
    """If-Range with a mismatched ETag must serve the full file (200), not
    206 — the client's cached representation is stale so partial-content
    would corrupt the assembled response.

    Asymmetry vs `test_if_range_matching_etag_206` (same Range, matching
    ETag → 206) is what makes this contract meaningful; the lack of a
    `Content-Range` header is the additional discriminator over a path
    where core simply ignored the Range header.
    """
    zst = STATIC_SIDECAR.read_bytes()
    r, body = http_request(
        static_range_nginx, "/static-range/payload",
        accept_encoding="zstd",
        headers={"Range": "bytes=0-99", "If-Range": '"deadbeef-nope"'},
    )
    assert r.status_code == 200, (
        f"status={r.status_code}, want 200 (If-Range mismatched → full body)"
    )
    assert r.headers.get("Content-Range") is None, (
        f"Content-Range must be absent on If-Range-mismatch 200; "
        f"got {r.headers.get('Content-Range')!r}"
    )
    assert body == zst, (
        f"body mismatch: got {len(body)} bytes, want {len(zst)}"
    )
