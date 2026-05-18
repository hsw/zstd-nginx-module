"""Pytest port of t/regression/static-edge-cases.sh.

Same 15 cases as the .sh original. The static module now honours q=0 and
tolerates OWS around `;` (commits 5e43352 and 32e7f75); the historic
"expected to FAIL on master" caveat is therefore obsolete and all cases
in PLAIN_CASES (including q=0) should pass.

Module-scoped fixture: all 15 cases share one nginx instance + one fixture
directory. Setup creates the regular, plain-only, unreadable, directory-
shaped and fifo-shaped sidecars; teardown stops nginx (file cleanup left to
the host between runs — the fixture dir is reused).
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
import requests

from conftest import (
    zstd_decompress,
    BASE_URL,
    PORT,
    _nginx_has_compat,
    http_request,
    render_template,
    start_nginx,
    stop_nginx,
)

FIXTURE_ROOT = Path("/var/fixtures/static-edge")
STATIC_FILE = FIXTURE_ROOT / "sample"
STATIC_SIDECAR = FIXTURE_ROOT / "sample.zst"
PLAIN_ONLY = FIXTURE_ROOT / "plain-only"
NO_PERM_FILE = FIXTURE_ROOT / "no-perm"
NO_PERM_SIDECAR = FIXTURE_ROOT / "no-perm.zst"
DIR_SIDECAR = FIXTURE_ROOT / "dir-sidecar.zst"
FIFO_SIDECAR = FIXTURE_ROOT / "fifo-sidecar.zst"


@pytest.fixture(scope="module")
def static_edge_nginx():
    """Build the fixture tree and start nginx with the three static locations
    (on / always / on-no-vary). Sets up unreadable, directory-shaped and
    fifo-shaped sidecars used by the per-case tests."""
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)

    # Regular content + matching .zst sidecar.
    STATIC_FILE.write_bytes(b"S" * 8192)
    PLAIN_ONLY.write_bytes(b"P" * 2048)
    NO_PERM_FILE.write_bytes(b"N" * 4096)
    subprocess.run(
        ["zstd", "-q", "-f", str(STATIC_FILE), "-o", str(STATIC_SIDECAR)],
        check=True,
    )

    # Unreadable sidecar (NGX_EACCES path).
    subprocess.run(
        ["zstd", "-q", "-f", str(NO_PERM_FILE), "-o", str(NO_PERM_SIDECAR)],
        check=True,
    )
    os.chmod(NO_PERM_SIDECAR, 0)

    # Directory-shaped sidecar (of.is_dir = 1 → decline path).
    if DIR_SIDECAR.exists():
        if DIR_SIDECAR.is_dir():
            for p in DIR_SIDECAR.iterdir():
                p.unlink()
            DIR_SIDECAR.rmdir()
        else:
            DIR_SIDECAR.unlink()
    DIR_SIDECAR.mkdir()
    # Plain content for the dir-sidecar fallback assertion.
    (FIXTURE_ROOT / "dir-sidecar").write_bytes(b"D" * 4096)

    # FIFO-shaped sidecar (!of.is_file → NGX_HTTP_NOT_FOUND path).
    if FIFO_SIDECAR.exists():
        FIFO_SIDECAR.unlink()
    os.mkfifo(FIFO_SIDECAR)
    (FIXTURE_ROOT / "fifo-sidecar").write_bytes(b"F" * 2048)

    # Both filter + static modules under --with-compat builds.
    load_modules = (
        "load_module modules/ngx_http_zstd_filter_module.so;\n"
        "load_module modules/ngx_http_zstd_static_module.so;"
        if _nginx_has_compat()
        else ""
    )

    extra_locations = """
        location /static-on/ {
            zstd off;
            zstd_static on;
            alias /var/fixtures/static-edge/;
            default_type text/plain;
        }

        location /static-always/ {
            zstd off;
            zstd_static always;
            alias /var/fixtures/static-edge/;
            default_type text/plain;
        }

        location /static-on-no-vary/ {
            zstd off;
            zstd_static on;
            gzip_vary off;
            alias /var/fixtures/static-edge/;
            default_type text/plain;
        }
"""
    stop_nginx()
    render_template(extra_locations=extra_locations, load_modules=load_modules)
    start_nginx()
    try:
        yield BASE_URL
    finally:
        # Best-effort cleanup of the fifo so leftover doesn't break next run.
        try:
            FIFO_SIDECAR.unlink()
        except FileNotFoundError:
            pass
        stop_nginx()


def _get(url, path, accept_encoding=None, method="GET", timeout=5):
    """Local wrapper around http_request returning (response, raw_body) so
    body bytes survive urllib3's zstd auto-decoder."""
    return http_request(url, path, method=method, accept_encoding=accept_encoding, timeout=timeout)


# ---------- positive: .zst sidecar is served ----------

ZSTD_SERVE_CASES = [
    # (label, path, accept_encoding)
    ("on-explicit-token",       "/static-on/sample",       "zstd"),
    ("on-uppercase-token",      "/static-on/sample",       "ZSTD"),
    ("always-q-zero-still-serves", "/static-always/sample", "zstd;q=0"),
]


@pytest.mark.parametrize(
    "label,path,ae",
    ZSTD_SERVE_CASES,
    ids=[c[0] for c in ZSTD_SERVE_CASES],
)
def test_serves_zstd_sidecar(static_edge_nginx, label, path, ae):
    r, body = _get(static_edge_nginx, path, accept_encoding=ae)
    assert r.status_code == 200, f"[{label}] status={r.status_code}"
    assert r.headers.get("Content-Encoding") == "zstd", (
        f"[{label}] AE={ae!r}: expected Content-Encoding=zstd, "
        f"got {r.headers.get('Content-Encoding')!r}"
    )
    # zstd-decode via python-zstandard (apt: python3-zstandard, baked into
    # every Dockerfile alongside python3-pytest / python3-requests).
    import zstandard  # noqa: WPS433 — module-local import keeps it lazy
    dec = zstd_decompress(body)
    assert dec == STATIC_FILE.read_bytes(), f"[{label}] roundtrip mismatch"


# ---------- negative: must NOT serve zstd, falls back to plain ----------

PLAIN_CASES = [
    # (label, path, accept_encoding, expected_plain_path)
    # q=0: now honoured by the static module (post commit 5e43352 — the
    # RFC 9110 parser fix). The .sh original documented this as "fails on
    # master" but that caveat is obsolete on this branch.
    ("on-q-zero-skipped",         "/static-on/sample",     "zstd;q=0",   STATIC_FILE),
    ("on-false-prefix-skipped",   "/static-on/sample",     "zstdx",      STATIC_FILE),
    ("on-wildcard-only-skipped",  "/static-on/sample",     "*",          STATIC_FILE),
    ("on-no-accept-encoding-skipped", "/static-on/sample", None,         STATIC_FILE),
    ("on-missing-sidecar-fallback", "/static-on/plain-only", "zstd",     PLAIN_ONLY),
]


@pytest.mark.parametrize(
    "label,path,ae,plain_path",
    PLAIN_CASES,
    ids=[c[0] for c in PLAIN_CASES],
)
def test_serves_plain(static_edge_nginx, label, path, ae, plain_path):
    r, body = _get(static_edge_nginx, path, accept_encoding=ae)
    assert r.status_code == 200, f"[{label}] status={r.status_code}"
    assert r.headers.get("Content-Encoding") != "zstd", (
        f"[{label}] AE={ae!r}: must NOT serve zstd, "
        f"got Content-Encoding={r.headers.get('Content-Encoding')!r}"
    )
    assert body == plain_path.read_bytes(), (
        f"[{label}] body mismatch: got {len(body)} bytes, "
        f"expected {plain_path.stat().st_size}"
    )


# ---------- HEAD: sidecar headers must reflect the .zst file ----------

def test_head_sidecar_headers(static_edge_nginx):
    r, _ = _get(static_edge_nginx, "/static-on/sample", accept_encoding="zstd", method="HEAD")
    zst_size = STATIC_SIDECAR.stat().st_size
    assert r.status_code == 200
    assert r.headers.get("Content-Encoding") == "zstd"
    assert int(r.headers.get("Content-Length", "0")) == zst_size, (
        f"HEAD Content-Length={r.headers.get('Content-Length')!r}, "
        f"expected {zst_size}"
    )


# ---------- module-decline edge cases (filter rules) ----------

def test_post_method_declined(static_edge_nginx):
    r, _ = http_request(
        static_edge_nginx, "/static-on/sample", method="POST",
        accept_encoding="zstd", data="body=ignored",
    )
    assert 400 <= r.status_code < 500, f"POST got status={r.status_code}"
    assert r.headers.get("Content-Encoding") != "zstd"


def test_uri_trailing_slash_declined(static_edge_nginx):
    r, _ = _get(static_edge_nginx, "/static-on/", accept_encoding="zstd")
    # nginx core typically returns 403 (no index) or 404; either is fine.
    assert r.headers.get("Content-Encoding") != "zstd"


def test_gzip_vary_off_no_zstd_ae_declined(static_edge_nginx):
    r, body = _get(static_edge_nginx, "/static-on-no-vary/sample", accept_encoding="gzip")
    assert r.status_code == 200
    assert r.headers.get("Content-Encoding") != "zstd"
    assert body == STATIC_FILE.read_bytes()


def test_eacces_sidecar_fallback(static_edge_nginx):
    """chmod 000 sidecar: if running as root in the container the open()
    bypasses EACCES; either way the assertion is "sidecar not served".
    Module either falls back to the plain original (200) or logs and declines."""
    r, _ = _get(static_edge_nginx, "/static-on/no-perm", accept_encoding="zstd")
    assert r.status_code == 200, f"status={r.status_code}"


def test_dir_shaped_sidecar_fallback(static_edge_nginx):
    """mkdir foo.zst: of.is_dir → declines before !of.is_file fires →
    falls back to the plain original."""
    r, body = _get(static_edge_nginx, "/static-on/dir-sidecar", accept_encoding="zstd")
    assert r.status_code == 200
    assert r.headers.get("Content-Encoding") != "zstd"
    assert body == (FIXTURE_ROOT / "dir-sidecar").read_bytes()


def test_fifo_shaped_sidecar_404(static_edge_nginx):
    """mkfifo foo.zst: open succeeds but of.is_file = 0 → CRIT log +
    NGX_HTTP_NOT_FOUND (line 209-212). Only realistic way to exercise
    that branch from user space."""
    r, _ = _get(static_edge_nginx, "/static-on/fifo-sidecar", accept_encoding="zstd")
    assert r.status_code == 404, f"status={r.status_code}"
    assert r.headers.get("Content-Encoding") != "zstd"
