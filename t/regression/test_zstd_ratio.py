"""Pytest port of t/regression/zstd-ratio.sh.

$zstd_ratio variable: only populated after the body filter finishes —
access_log is the reliable observation point. Headers are too early.

Coverage:
  * compressed response: access_log line matches `200 zstd <N.NNN>`
  * compressed body still round-trips byte-identically
  * uncompressed response: access_log line is `200 - -`
"""

from __future__ import annotations

import re
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

FIXTURE_ROOT = Path("/var/fixtures/zstd-ratio")
RATIO_BODY = FIXTURE_ROOT / "body"
LOG_PATH = Path("/tmp/zstd-ratio.log")


@pytest.fixture(scope="module")
def ratio_nginx():
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    RATIO_BODY.write_bytes(b"R" * 65536)

    load_modules = (
        "load_module modules/ngx_http_zstd_filter_module.so;"
        if _nginx_has_compat() else ""
    )
    extra_directives = (
        'log_format zstd_ratio_test "$status $sent_http_content_encoding $zstd_ratio";'
    )
    extra_locations = """
        access_log /tmp/zstd-ratio.log zstd_ratio_test;

        location = /ratio-body {
            alias /var/fixtures/zstd-ratio/body;
            default_type text/plain;
        }
"""
    stop_nginx()
    render_template(
        extra_directives=extra_directives,
        extra_locations=extra_locations,
        load_modules=load_modules,
    )
    start_nginx()
    try:
        yield BASE_URL
    finally:
        stop_nginx()


def _last_log_line(timeout_s: float = 3.0) -> str:
    """Tail /tmp/zstd-ratio.log until a line appears or timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            txt = LOG_PATH.read_text().rstrip("\n")
        except FileNotFoundError:
            txt = ""
        if txt:
            return txt.splitlines()[-1]
        time.sleep(0.1)
    return ""


def _clear_log() -> None:
    LOG_PATH.write_text("")


def test_compressed_log_ratio_and_roundtrip(ratio_nginx):
    _clear_log()
    r, body = http_request(ratio_nginx, "/ratio-body", accept_encoding="zstd")
    assert r.status_code == 200
    assert r.headers.get("Content-Encoding") == "zstd"
    line = _last_log_line()
    assert re.match(r"^200 zstd \d+\.\d{3}$", line), (
        f"access_log line doesn't match `200 zstd <N.NNN>` template: {line!r}"
    )

    dec = zstd_decompress(body)
    assert dec == RATIO_BODY.read_bytes(), (
        f"decoded body differs: dec={len(dec)} orig={RATIO_BODY.stat().st_size}"
    )


def test_plain_log_no_ratio(ratio_nginx):
    _clear_log()
    r, body = http_request(ratio_nginx, "/ratio-body", accept_encoding=None)
    assert r.status_code == 200
    assert r.headers.get("Content-Encoding") in (None, "")
    line = _last_log_line()
    assert line == "200 - -", (
        f"plain access_log line should be `200 - -`, got {line!r}"
    )
    assert body == RATIO_BODY.read_bytes(), "plain body must round-trip identically"
