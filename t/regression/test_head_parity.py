"""Pytest port of t/regression/head-parity.sh.

Regression for Task 6: HEAD and GET must return identical headers
(Content-Encoding: zstd, Vary: Accept-Encoding, no Content-Length) and HEAD
must NOT carry a body. ETag value must match between HEAD and GET on a
file-backed fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import zstandard

from conftest import (
    zstd_decompress,
    BASE_URL,
    http_request,
    render_template,
    start_nginx,
    stop_nginx,
)

ETAG_FIXTURE = Path("/var/fixtures/random/etag-fixture")


@pytest.fixture(scope="module")
def head_nginx():
    # Default template — /text fixture (180-byte repetitive body) is what the
    # HEAD/GET parity is asserted on. Etag fixture lives in /var/fixtures/random/
    # served by the template's /random/ alias.
    ETAG_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    if not ETAG_FIXTURE.exists() or ETAG_FIXTURE.stat().st_size == 0:
        ETAG_FIXTURE.write_bytes(b"Z" * 2048)
    stop_nginx()
    render_template()
    start_nginx()
    try:
        yield BASE_URL
    finally:
        stop_nginx()


def test_head_content_encoding(head_nginx):
    r, _ = http_request(head_nginx, "/text", method="HEAD", accept_encoding="zstd")
    assert r.headers.get("Content-Encoding") == "zstd"


def test_head_vary(head_nginx):
    r, _ = http_request(head_nginx, "/text", method="HEAD", accept_encoding="zstd")
    assert r.headers.get("Vary") == "Accept-Encoding"


def test_head_no_content_length(head_nginx):
    """Encoded length is unknown at header time when compression streams;
    nginx must switch to chunked (HTTP/1.1) and drop Content-Length."""
    r, _ = http_request(head_nginx, "/text", method="HEAD", accept_encoding="zstd")
    assert "Content-Length" not in r.headers, (
        f"HEAD with zstd must drop Content-Length, got {r.headers.get('Content-Length')!r}"
    )


def test_head_empty_body(head_nginx):
    """RFC 9110 §9.3.2: HEAD must have no body. An over-zealous body filter
    could emit a terminating chunk (5 bytes: "0\\r\\n\\r\\n") which we'd see here."""
    r, body = http_request(head_nginx, "/text", method="HEAD", accept_encoding="zstd")
    assert body == b"", f"HEAD body is {len(body)} bytes, want 0"


def test_get_content_encoding(head_nginx):
    r, _ = http_request(head_nginx, "/text", accept_encoding="zstd")
    assert r.headers.get("Content-Encoding") == "zstd"


def test_get_vary(head_nginx):
    r, _ = http_request(head_nginx, "/text", accept_encoding="zstd")
    assert r.headers.get("Vary") == "Accept-Encoding"


def test_get_no_content_length(head_nginx):
    r, _ = http_request(head_nginx, "/text", accept_encoding="zstd")
    assert "Content-Length" not in r.headers


def test_get_decompresses(head_nginx):
    r, body = http_request(head_nginx, "/text", accept_encoding="zstd")
    dec = zstd_decompress(body)
    assert len(dec) > 0, "decoded body is empty"


def test_etag_parity(head_nginx):
    """File-backed fixture so nginx core stamps a weak ETag from mtime+size.
    HEAD and GET must carry the SAME ETag value — zstd's header filter
    weakens strong ETags by prepending W/, applied identically on both."""
    head, _ = http_request(head_nginx, "/random/etag-fixture", method="HEAD", accept_encoding="zstd")
    get, _ = http_request(head_nginx, "/random/etag-fixture", accept_encoding="zstd")
    head_etag = head.headers.get("ETag", "")
    get_etag = get.headers.get("ETag", "")
    assert head_etag, "HEAD response missing ETag"
    assert get_etag, "GET response missing ETag"
    assert head_etag == get_etag, (
        f"HEAD ETag={head_etag!r} != GET ETag={get_etag!r}"
    )
