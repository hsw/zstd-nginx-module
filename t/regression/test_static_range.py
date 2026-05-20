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
  5. `bytes=999999-` (EOF)  → 416
  6. No Range request       → Accept-Ranges: bytes header present
  7. If-Range matching etag → 206; mismatched → 200 full
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import requests

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
    # 64 KiB pseudo-random-ish payload (highly compressible "S"+counter so the
    # .zst sidecar is non-trivial in size — we need >999999 to test the
    # beyond-EOF case as well, but 416 fires whenever the start offset is
    # >= file size, so any size that's smaller than 999999 works).
    STATIC_FILE.write_bytes(b"".join(
        bytes([(i * 31) & 0xff]) for i in range(65536)
    ))
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


def _get_with_headers(url, path, headers, timeout=5):
    """Tiny requests wrapper that disables auto-decompression and gives us
    raw bytes — http_request() in conftest doesn't accept arbitrary headers
    (only Accept-Encoding), so the Range tests go direct."""
    r = requests.get(url + path, headers=headers, stream=True, timeout=timeout)
    body = r.raw.read(decode_content=False)
    r.close()
    return r, body


# ---------- helpers: the .zst sidecar bytes are the canonical truth ----------

def _zst_bytes() -> bytes:
    return STATIC_SIDECAR.read_bytes()


def _zst_size() -> int:
    return STATIC_SIDECAR.stat().st_size


# ---------- case 1: prefix range ----------

def test_range_first_100_bytes(static_range_nginx):
    zst = _zst_bytes()
    r, body = _get_with_headers(
        static_range_nginx, "/static-range/payload",
        headers={"Accept-Encoding": "zstd", "Range": "bytes=0-99"},
    )
    assert r.status_code == 206, f"status={r.status_code}, want 206"
    assert r.headers.get("Content-Encoding") == "zstd"
    assert body == zst[:100], (
        f"body mismatch: got {len(body)} bytes, want 100"
    )
    cr = r.headers.get("Content-Range")
    assert cr == f"bytes 0-99/{len(zst)}", f"Content-Range={cr!r}"


# ---------- case 2: open-ended range ----------

def test_range_tail_from_offset(static_range_nginx):
    zst = _zst_bytes()
    r, body = _get_with_headers(
        static_range_nginx, "/static-range/payload",
        headers={"Accept-Encoding": "zstd", "Range": "bytes=100-"},
    )
    assert r.status_code == 206, f"status={r.status_code}, want 206"
    assert r.headers.get("Content-Encoding") == "zstd"
    assert body == zst[100:], (
        f"body mismatch: got {len(body)} bytes, want {len(zst) - 100}"
    )
    cr = r.headers.get("Content-Range")
    assert cr == f"bytes 100-{len(zst) - 1}/{len(zst)}", f"Content-Range={cr!r}"


# ---------- case 3: suffix range ----------

def test_range_suffix_last_50(static_range_nginx):
    zst = _zst_bytes()
    r, body = _get_with_headers(
        static_range_nginx, "/static-range/payload",
        headers={"Accept-Encoding": "zstd", "Range": "bytes=-50"},
    )
    assert r.status_code == 206, f"status={r.status_code}, want 206"
    assert r.headers.get("Content-Encoding") == "zstd"
    assert body == zst[-50:], (
        f"body mismatch: got {len(body)} bytes, want 50"
    )
    cr = r.headers.get("Content-Range")
    assert cr == f"bytes {len(zst) - 50}-{len(zst) - 1}/{len(zst)}", (
        f"Content-Range={cr!r}"
    )


# ---------- case 4: invalid order range -> 416 ----------

def test_range_invalid_order_416(static_range_nginx):
    """bytes=10-9: start > end → 416 Requested Range Not Satisfiable."""
    r, _ = _get_with_headers(
        static_range_nginx, "/static-range/payload",
        headers={"Accept-Encoding": "zstd", "Range": "bytes=10-9"},
    )
    assert r.status_code == 416, f"status={r.status_code}, want 416"


# ---------- case 5: range beyond EOF -> 416 ----------

def test_range_beyond_eof_416(static_range_nginx):
    """bytes=999999-: start beyond file size → 416."""
    zst_size = _zst_size()
    assert zst_size < 999999, (
        f"fixture too large ({zst_size}); test assumes <999999 bytes"
    )
    r, _ = _get_with_headers(
        static_range_nginx, "/static-range/payload",
        headers={"Accept-Encoding": "zstd", "Range": "bytes=999999-"},
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
    assert body == _zst_bytes()


# ---------- case 7: If-Range matching vs mismatched ----------

def test_if_range_matching_etag_206(static_range_nginx):
    """If-Range with the current ETag must succeed → 206 partial content."""
    # Warmup to learn the current ETag.
    warm, _ = http_request(
        static_range_nginx, "/static-range/payload", accept_encoding="zstd",
    )
    etag = warm.headers.get("ETag")
    assert etag, "expected ETag on sidecar response"

    r, body = _get_with_headers(
        static_range_nginx, "/static-range/payload",
        headers={
            "Accept-Encoding": "zstd",
            "Range": "bytes=0-99",
            "If-Range": etag,
        },
    )
    assert r.status_code == 206, (
        f"status={r.status_code}, want 206 (If-Range matched)"
    )
    assert body == _zst_bytes()[:100]


def test_if_range_mismatched_etag_200_full(static_range_nginx):
    """If-Range with a mismatched ETag must serve the full file (200), not
    206 — the client's cached representation is stale so partial-content
    would corrupt the assembled response."""
    r, body = _get_with_headers(
        static_range_nginx, "/static-range/payload",
        headers={
            "Accept-Encoding": "zstd",
            "Range": "bytes=0-99",
            "If-Range": '"deadbeef-nope"',
        },
    )
    assert r.status_code == 200, (
        f"status={r.status_code}, want 200 (If-Range mismatched → full body)"
    )
    assert body == _zst_bytes(), (
        f"body mismatch: got {len(body)} bytes, want {_zst_size()}"
    )
