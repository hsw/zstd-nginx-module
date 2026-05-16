"""Pytest port of t/regression/filter-eligibility.sh.

Coverage for the zstd header-filter gate conditions:
  * zstd_min_length: short body skipped, long body encoded
  * zstd_types: matching MIME encoded, non-matching skipped
  * status gate: 404 encoded, 500 skipped
  * pre-encoded upstream (Content-Encoding: gzip already set) → not re-encoded
  * tiny zstd_buffers on incompressible random body still round-trips

The pre-encoded upstream case spawns a tiny python TCP server on port 9000
inside the container (matches the template's /origin/ proxy_pass target).
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import zstandard

from conftest import (
    zstd_decompress,
    BASE_URL,
    _nginx_has_compat,
    http_request,
    render_template,
    start_nginx,
    stop_nginx,
)

FIXTURE_ROOT = Path("/var/fixtures/filter-eligibility")
MIN_LARGE = FIXTURE_ROOT / "min-large"
TYPE_PLAIN = FIXTURE_ROOT / "type-plain"
TYPE_JSON = FIXTURE_ROOT / "type-json"
BUFFER_PRESSURE = FIXTURE_ROOT / "buffer-pressure"


def _start_preencoded_fixture() -> threading.Thread:
    """Tiny TCP server on 127.0.0.1:9000 that always responds with a
    gzip-encoded body. The template's /origin/ proxies here so requests to
    /origin/ test "upstream already set Content-Encoding: gzip — filter must
    not re-encode"."""
    import gzip
    body = gzip.compress(b"already-gzip-encoded\n" * 256)

    def handle(c: socket.socket) -> None:
        try:
            c.settimeout(2)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = c.recv(4096)
                if not chunk:
                    break
                buf += chunk
            c.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Encoding: gzip\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n"
                b"\r\n" + body
            )
        finally:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()

    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 9000))
    s.listen(8)

    def loop():
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return  # socket closed
            threading.Thread(target=handle, args=(c,), daemon=True).start()

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    t._sock = s  # type: ignore[attr-defined]  — keep ref for shutdown
    return t


@pytest.fixture(scope="module")
def filter_eligibility_nginx():
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    MIN_LARGE.write_bytes(b"M" * 4096)
    TYPE_PLAIN.write_bytes(b"P" * 4096)
    TYPE_JSON.write_bytes(b'{"payload":"' + b"J" * 4096 + b'"}\n')
    BUFFER_PRESSURE.write_bytes(os.urandom(262144))

    fixture_thread = _start_preencoded_fixture()

    load_modules = (
        "load_module modules/ngx_http_zstd_filter_module.so;"
        if _nginx_has_compat() else ""
    )
    extra_locations = """
        location = /min-small {
            zstd_min_length 1024;
            default_type text/plain;
            return 200 "short-body\\n";
        }

        location = /min-large {
            zstd_min_length 1024;
            alias /var/fixtures/filter-eligibility/min-large;
            default_type text/plain;
        }

        location = /type-plain {
            zstd_types text/plain;
            alias /var/fixtures/filter-eligibility/type-plain;
            default_type text/plain;
        }

        location = /type-json {
            zstd_types text/plain;
            alias /var/fixtures/filter-eligibility/type-json;
            default_type application/json;
        }

        location = /status-404 {
            default_type text/plain;
            return 404 "not-found-not-found-not-found-not-found-not-found\\n";
        }

        location = /status-500 {
            default_type text/plain;
            return 500 "server-error-server-error-server-error-server-error\\n";
        }

        location = /buffer-pressure {
            zstd_buffers 2 1k;
            alias /var/fixtures/filter-eligibility/buffer-pressure;
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
        # tear down the fixture server.
        try:
            fixture_thread._sock.close()  # type: ignore[attr-defined]
        except Exception:
            pass


# (label, path, expected_status, expected_plain_body)
ZSTD_EXPECTED = [
    ("min-length-large-encoded",      "/min-large",        200, MIN_LARGE),
    ("type-plain-encoded",            "/type-plain",       200, TYPE_PLAIN),
    ("status-404-encoded",            "/status-404",       404, None),  # synthetic body
    ("buffer-pressure-roundtrip",     "/buffer-pressure",  200, BUFFER_PRESSURE),
]


@pytest.mark.parametrize(
    "label,path,expected_status,plain_path",
    ZSTD_EXPECTED,
    ids=[c[0] for c in ZSTD_EXPECTED],
)
def test_expect_zstd(filter_eligibility_nginx, label, path, expected_status, plain_path):
    r, body = http_request(filter_eligibility_nginx, path, accept_encoding="zstd")
    assert r.status_code == expected_status, f"[{label}] status={r.status_code}"
    assert r.headers.get("Content-Encoding") == "zstd", (
        f"[{label}] expected Content-Encoding=zstd, "
        f"got {r.headers.get('Content-Encoding')!r}"
    )
    dec = zstd_decompress(body)
    if plain_path is not None:
        assert dec == plain_path.read_bytes(), (
            f"[{label}] decoded body differs from {plain_path}"
        )
    else:
        assert len(dec) > 0, f"[{label}] decoded body is empty"


# (label, path, expected_status)
NO_ZSTD_EXPECTED = [
    ("min-length-small-skipped", "/min-small",  200),
    ("type-json-skipped",        "/type-json",  200),
    ("status-500-skipped",       "/status-500", 500),
]


@pytest.mark.parametrize(
    "label,path,expected_status",
    NO_ZSTD_EXPECTED,
    ids=[c[0] for c in NO_ZSTD_EXPECTED],
)
def test_expect_no_zstd(filter_eligibility_nginx, label, path, expected_status):
    r, _ = http_request(filter_eligibility_nginx, path, accept_encoding="zstd")
    assert r.status_code == expected_status, f"[{label}] status={r.status_code}"
    assert r.headers.get("Content-Encoding") != "zstd", (
        f"[{label}] Content-Encoding={r.headers.get('Content-Encoding')!r}, "
        f"must not be zstd"
    )


def test_preencoded_upstream_not_double_encoded(filter_eligibility_nginx):
    """Upstream already returned Content-Encoding: gzip — filter must NOT
    overwrite it. The /origin/ location proxies to the python fixture above."""
    r, _ = http_request(filter_eligibility_nginx, "/origin/", accept_encoding="zstd")
    assert r.status_code == 200, f"status={r.status_code}"
    assert r.headers.get("Content-Encoding") == "gzip", (
        f"expected gzip (upstream-set), got {r.headers.get('Content-Encoding')!r}"
    )
