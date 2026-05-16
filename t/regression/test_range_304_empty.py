"""Pytest port of t/regression/range-304-empty.sh.

Coverage for three rarely-exercised filter gates:
  * Range requests over a compressed body — module must NOT serve 206 + zstd
    (compressed partial-content is meaningless: byte offsets in Range don't
    map to encoded bytes).
  * 304 Not Modified short-circuit — conditional GET must not emit
    Content-Encoding and must not send a body.
  * Empty body (204 / explicit return 200 "") — body filter sees last_buf
    with bytes_in=0; must not crash, must not emit Content-Encoding.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (
    zstd_decompress,
    BASE_URL,
    _nginx_has_compat,
    http_request,
    render_template,
    start_nginx,
    stop_nginx,
)

FIXTURE_ROOT = Path("/var/fixtures/range-304-empty")
RANGE_BODY = FIXTURE_ROOT / "body"


@pytest.fixture(scope="module")
def range_nginx():
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    RANGE_BODY.write_bytes(b"R" * 65536)

    load_modules = (
        "load_module modules/ngx_http_zstd_filter_module.so;"
        if _nginx_has_compat() else ""
    )

    extra_locations = """
        location = /range-body {
            alias /var/fixtures/range-304-empty/body;
            default_type text/plain;
        }

        location = /empty-204 {
            return 204;
        }

        location = /empty-200 {
            default_type text/plain;
            return 200 "";
        }
"""
    stop_nginx()
    render_template(extra_locations=extra_locations, load_modules=load_modules)
    start_nginx()
    try:
        yield BASE_URL
    finally:
        stop_nginx()


def test_range_not_compressed(range_nginx):
    """Acceptable outcomes:
       (a) 200 + Content-Encoding: zstd (filter compressed, Range dropped)
       (b) 206 + no Content-Encoding    (Range served, compression bypassed)
       (c) 200 + no Content-Encoding    (also fine)
    Forbidden: 206 + zstd (partial-content over compressed bytes makes no
    sense)."""
    # Range can't piggyback on http_request's auto identity Accept-Encoding,
    # so we set it manually and add Range as second header by going through
    # http_request and then a manual override. Helper takes only one AE arg;
    # build via requests directly here, but disable urllib3 auto-decode.
    import requests
    r = requests.get(
        range_nginx + "/range-body",
        headers={"Accept-Encoding": "zstd", "Range": "bytes=0-1023"},
        stream=True, timeout=5,
    )
    body = r.raw.read(decode_content=False)
    r.close()
    if r.status_code == 206 and r.headers.get("Content-Encoding") == "zstd":
        pytest.fail(
            f"got 206 + Content-Encoding=zstd (compressed partial-content "
            f"is meaningless); Content-Range={r.headers.get('Content-Range')}"
        )


def test_304_not_modified(range_nginx):
    """Conditional GET that matches the warmup ETag must:
      * status 304
      * no Content-Encoding
      * empty body
    """
    # Warmup to learn the ETag (filter may weakify it under compression).
    warm, _ = http_request(range_nginx, "/range-body", accept_encoding="zstd")
    etag = warm.headers.get("ETag")
    if not etag:
        pytest.skip("upstream did not return an ETag — can't test conditional")

    import requests
    r = requests.get(
        range_nginx + "/range-body",
        headers={"Accept-Encoding": "zstd", "If-None-Match": etag},
        stream=True, timeout=5,
    )
    body = r.raw.read(decode_content=False)
    r.close()
    assert r.status_code == 304, f"status={r.status_code}, want 304"
    assert r.headers.get("Content-Encoding") in (None, ""), (
        f"304 must not set Content-Encoding, got {r.headers.get('Content-Encoding')!r}"
    )
    assert body == b"", f"304 must have empty body, got {len(body)} bytes"


def test_empty_204_no_encoding(range_nginx):
    """204 No Content: filter must skip; no Content-Encoding, empty body."""
    r, body = http_request(range_nginx, "/empty-204", accept_encoding="zstd")
    assert r.status_code == 204, f"status={r.status_code}"
    assert r.headers.get("Content-Encoding") in (None, "")
    assert body == b""


def test_empty_200_no_crash(range_nginx):
    """200 with empty body: zero-byte body filter path. Most likely path to
    trigger UB in the compress loop (last_buf=1 with bytes_in=0). The check
    is nginx doesn't crash; if it does emit Content-Encoding the decompressed
    body must still be empty."""
    r, body = http_request(range_nginx, "/empty-200", accept_encoding="zstd")
    assert r.status_code == 200, f"status={r.status_code}"

    ce = r.headers.get("Content-Encoding")
    if ce == "zstd":
        # encoded empty body must decompress to empty
        import zstandard
        dec = zstd_decompress(body)
        assert dec == b"", f"zstd-encoded empty body decoded to {len(dec)} bytes"
    else:
        assert body == b"", f"plain empty-200 must have empty body, got {len(body)}"
