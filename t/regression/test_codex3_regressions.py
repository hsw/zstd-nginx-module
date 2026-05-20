"""TDD reproductions for the codex3 review findings (docs/codex3.md).

Each test below is currently xfail(strict=True) — the bug it captures is
not yet fixed. When the fix lands the xfail flips strict-pass and the
marker is removed.

Bugs covered:

  * P2.1 — `zstd_dict_file` is silently skipped in a child location
    whose parent merged with `enable=0` and whose own `zstd_comp_level`
    equals the parent's default. The merge in
    filter/ngx_http_zstd_filter_module.c:1478-1481 assigns
    `conf->dict = prev->dict` when levels match, but the parent (off)
    never loaded the dict, so `prev->dict == NULL` and the child runs
    without the configured dictionary.

  * P2.2 — `zstd_static on` calls `ngx_http_zstd_ok()` BEFORE the
    `.zst` sidecar probe. `ngx_http_zstd_ok()` sets
    `r->gzip_tested = 1; r->gzip_ok = 0;` so when the sidecar is
    absent and the handler returns NGX_DECLINED
    (static/ngx_http_zstd_static_module.c:164-188), nginx core's gzip
    filter sees `gzip_tested=1` and skips compression even though the
    client sent `Accept-Encoding: gzip, zstd` and the operator
    configured `gzip on`.

Both tests build their own minimal config via `render_template` extras
and rely on the standard regression helpers in conftest.py.
"""

from __future__ import annotations

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

LOG_PATH = Path("/tmp/nginx-codex3.log")


# ---------------------------------------------------------------------------
# P2.1 — dict inheritance bug
# ---------------------------------------------------------------------------

DICT_FILE = Path("/var/fixtures/codex3/zstd.dict")
DICT_FIXTURE_DIR = Path("/var/fixtures/codex3/payload")
DICT_PAYLOAD = DICT_FIXTURE_DIR / "page.html"


def _load_modules_filter_and_static() -> str:
    if not _nginx_has_compat():
        return ""
    return (
        "load_module modules/ngx_http_zstd_filter_module.so;\n"
        "load_module modules/ngx_http_zstd_static_module.so;"
    )


def _setup_dict_fixtures() -> None:
    DICT_FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    # Any 4 KiB blob is enough — ZSTD_createCDict_byReference does not
    # require a trained dict format to allocate a CDict, and the test
    # asserts on the "auto-window: skipped (dict configured)" log line
    # which fires whenever zlcf->dict != NULL.
    DICT_FILE.write_bytes(b"D" * 4096)
    # A payload large enough to traverse the body filter on every variant.
    DICT_PAYLOAD.write_bytes(b"The quick brown fox jumps over the lazy dog.\n" * 64)


def _start_with_log(extra_directives: str, extra_server: str = "",
                    extra_locations: str = "") -> None:
    if LOG_PATH.exists():
        LOG_PATH.unlink()
    directives = (
        f"error_log {LOG_PATH} info;\n"
        + extra_directives
    )
    render_template(
        extra_directives=directives,
        extra_server=extra_server,
        extra_locations=extra_locations,
        load_modules=_load_modules_filter_and_static(),
    )
    start_nginx()


@pytest.fixture
def dict_inherit_nginx():
    """Reproduce the P2.1 dict-inheritance bug: http has zstd_dict_file,
    server turns zstd off, location turns it back on with the inherited
    default level (1). The merge path at filter:1478-1481 will assign
    `conf->dict = prev->dict` (= NULL) because levels match.

    A second location pins comp_level=5 so the merge code takes the
    "load dict from file" branch and we have a positive control that
    the dict-load path itself works.
    """
    _setup_dict_fixtures()

    extra_directives = (
        f"zstd_dict_file {DICT_FILE};\n"
        # zstd_min_length 0 / zstd_types * already in the template at
        # http context, so the dict-inherit location inherits them.
    )

    # zstd off at server context drives the merge into the bug branch:
    # any location under this server that turns zstd back on with the
    # inherited default level (1) gets conf->dict = prev->dict = NULL.
    extra_server = "zstd off;"

    extra_locations = f"""
        location /dict-inherit/ {{
            zstd on;
            alias {DICT_FIXTURE_DIR}/;
            default_type text/plain;
        }}

        location /dict-direct/ {{
            zstd on;
            zstd_comp_level 5;
            alias {DICT_FIXTURE_DIR}/;
            default_type text/plain;
        }}
"""
    stop_nginx()
    _start_with_log(
        extra_directives=extra_directives,
        extra_server=extra_server,
        extra_locations=extra_locations,
    )
    try:
        yield BASE_URL
    finally:
        stop_nginx()


def _read_log() -> str:
    return LOG_PATH.read_text() if LOG_PATH.exists() else ""


def test_dict_direct_uses_configured_dict(dict_inherit_nginx):
    """Positive control: location with an explicit zstd_comp_level that
    differs from the parent forces the merge code into the load-from-
    file branch (filter:1489-1571), so zlcf->dict is non-NULL and the
    body filter emits the dict-skipped auto-window marker.

    If this test ever goes red it means the dict load path itself is
    broken — the dict-inherit xfail below is meaningless until this
    passes."""
    r, body = http_request(
        dict_inherit_nginx, "/dict-direct/page.html",
        accept_encoding="zstd",
    )
    assert r.status_code == 200, f"status={r.status_code}"
    assert r.headers.get("Content-Encoding") == "zstd", (
        f"got Content-Encoding={r.headers.get('Content-Encoding')!r}"
    )
    log = _read_log()
    assert "zstd auto-window: skipped (dict configured)" in log, (
        "dict-direct location did not load the dict — load-from-file "
        "branch broken? log tail:\n" + log[-2000:]
    )


def test_dict_inherited_through_off_parent(dict_inherit_nginx):
    """BUG reproduction: location uses the default comp_level (1) under
    a server that is `zstd off`. The merge path matches levels and
    inherits prev->dict, but prev->dict was never loaded because the
    parent was disabled. Result: compression runs without the
    configured dictionary.

    Detection signal: a working dict-on-location emits
    `zstd auto-window: skipped (dict configured)` at NGX_LOG_INFO. The
    buggy path emits the regular auto-window line (no dict marker)
    because zlcf->dict == NULL.
    """
    r, body = http_request(
        dict_inherit_nginx, "/dict-inherit/page.html",
        accept_encoding="zstd",
    )
    assert r.status_code == 200, f"status={r.status_code}"
    assert r.headers.get("Content-Encoding") == "zstd", (
        f"got Content-Encoding={r.headers.get('Content-Encoding')!r}"
    )
    log = _read_log()
    assert "zstd auto-window: skipped (dict configured)" in log, (
        "P2.1: child location did not inherit the configured dict. "
        "log tail:\n" + log[-2000:]
    )


def test_dict_inherited_through_on_off_on_chain(dict_inherit_nginx):
    """Inverse-shape sanity: http `zstd on` + dict_file, server
    `zstd off`, location `zstd on` (same default level=1). After the
    P2.1 fix the child reload-from-file branch fires (prev->dict is
    NULL because parent merged with enable=0), so the configured dict
    is loaded and the body filter emits the dict-skipped marker.

    Also verifies the fix doesn't double-load: each compressed request
    must emit the dict-skipped marker exactly once (one body-filter
    pass → one log line), proving we use a single CDict per
    request, not one per merge-level encountered on the conf chain.
    """
    # Truncate the log so we count markers from this request only.
    if LOG_PATH.exists():
        LOG_PATH.write_text("")

    r, body = http_request(
        dict_inherit_nginx, "/dict-inherit/page.html",
        accept_encoding="zstd",
    )
    assert r.status_code == 200, f"status={r.status_code}"
    assert r.headers.get("Content-Encoding") == "zstd", (
        f"got Content-Encoding={r.headers.get('Content-Encoding')!r}"
    )
    log = _read_log()
    marker = "zstd auto-window: skipped (dict configured)"
    count = log.count(marker)
    assert count == 1, (
        f"P2.1 inverse-shape: expected exactly one dict-skipped "
        f"marker per request, got {count}. log tail:\n"
        + log[-2000:]
    )


# ---------------------------------------------------------------------------
# P2.2 — static handler poisons gzip eligibility before .zst probe
# ---------------------------------------------------------------------------

GZIP_FIXTURE_DIR = Path("/var/fixtures/codex3/static-poison")
GZIP_PLAIN = GZIP_FIXTURE_DIR / "plain.txt"


def _setup_gzip_fixtures() -> None:
    GZIP_FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    # Highly repetitive so gzip output is well below the plain bytes —
    # makes "no compression at all" trivially distinguishable from
    # "compressed and decoded by the urllib3 layer".
    GZIP_PLAIN.write_bytes(b"hello hello hello\n" * 4096)
    # CRITICAL: no .zst sidecar — that is the bug precondition.
    sidecar = GZIP_PLAIN.with_suffix(GZIP_PLAIN.suffix + ".zst")
    if sidecar.exists():
        sidecar.unlink()


@pytest.fixture
def static_gzip_nginx():
    """Set up nginx with both gzip and zstd_static enabled. The location
    aliases a directory containing only a plain file (no .zst). A
    client sending `Accept-Encoding: gzip, zstd` should receive gzip
    because the zstd_static handler must decline cleanly when the
    sidecar is absent — but the bug poisons gzip eligibility before
    the file probe runs, so nginx returns identity instead."""
    _setup_gzip_fixtures()

    extra_directives = (
        # gzip side: compress everything, no min-length floor, so the
        # plain .txt response is unambiguously eligible. gzip_proxied
        # any keeps the test independent of upstream caching headers.
        "gzip on;\n"
        "gzip_types *;\n"
        "gzip_min_length 0;\n"
        "gzip_proxied any;\n"
    )

    # `zstd off` must go at server context — the template already
    # emits `zstd on;` at http context, and nginx rejects duplicate
    # flag directives in the same scope. Disabling at server keeps
    # the on-the-fly filter out of this test so the only zstd code
    # path exercised is the static handler.
    extra_server = "zstd off;"

    extra_locations = f"""
        location /static-poison/ {{
            zstd_static on;
            alias {GZIP_FIXTURE_DIR}/;
            default_type text/plain;
        }}
"""
    stop_nginx()
    _start_with_log(
        extra_directives=extra_directives,
        extra_server=extra_server,
        extra_locations=extra_locations,
    )
    try:
        yield BASE_URL
    finally:
        stop_nginx()


def test_gzip_survives_missing_zst_sidecar(static_gzip_nginx):
    """BUG reproduction: AE=gzip,zstd → .zst missing → expect gzip.

    Currently the static handler poisons gzip eligibility before the
    file probe; the response comes back as identity (no compression),
    which is a real loss of bytes-on-the-wire for clients that have
    BOTH gzip and zstd available."""
    r, body = http_request(
        static_gzip_nginx, "/static-poison/plain.txt",
        accept_encoding="gzip, zstd",
    )
    assert r.status_code == 200, f"status={r.status_code}"
    # Sanity: the plain file is much larger than its gzip output. If
    # gzip ran the body would be far smaller than the source.
    plain_size = GZIP_PLAIN.stat().st_size
    assert plain_size > 1024, "fixture sanity"
    enc = r.headers.get("Content-Encoding")
    assert enc == "gzip", (
        f"expected Content-Encoding=gzip when .zst is missing and "
        f"AE='gzip, zstd', got {enc!r}; "
        f"plain_size={plain_size} response_body_size={len(body)}"
    )


def test_gzip_works_when_zstd_static_off(static_gzip_nginx):
    """Negative control: same fixture but a path with no static
    handler binding — gzip must compress the plain file. This proves
    gzip is enabled in the test image and the fixture content is
    compressible, so the xfail above isolates the static-handler bug."""
    # The root location of the template runs gzip but doesn't bind
    # zstd_static, so a GET to a path the static module declines
    # (e.g. /text) must come back gzipped.
    r, body = http_request(
        static_gzip_nginx, "/text", accept_encoding="gzip",
    )
    assert r.status_code == 200
    assert r.headers.get("Content-Encoding") == "gzip", (
        "gzip not enabled in test image / template? "
        f"got headers={dict(r.headers)}"
    )
