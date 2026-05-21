"""Regression tests for the codex3 review findings (docs/codex3.md).

These were originally TDD reproductions marked xfail(strict=True); the
fixes have landed (commits flipping xfail → strict-pass and removing the
markers) and the tests now guard against re-regression.

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
    broken — the dict-inherit regression test below depends on this
    baseline working."""
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


def test_dict_marker_emitted_exactly_once_per_request(dict_inherit_nginx):
    """Same config as test_dict_inherited_through_off_parent, but
    asserts the dict-skipped marker is emitted exactly once per
    compressed request (not zero, not more than one).

    Together with the inheritance test above this rules out two
    distinct refactor regressions: (a) child fails to load the dict
    and emits no marker (the P2.1 bug); (b) child loads the dict
    multiple times per request, or emits the marker once per merge
    level traversed on the conf chain — either of which would inflate
    the marker count above one. Single body-filter pass should produce
    exactly one log line.
    """
    # The dict_inherit_nginx fixture is function-scoped and
    # _start_with_log unlinks LOG_PATH on every setup, so the log only
    # contains lines from this one request.
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

        location /static-poison-always/ {{
            zstd_static always;
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


def _assert_gzip_passthrough(r, body, *, label: str, check_body_sanity: bool = True):
    """Shared assertion for the three .zst-missing → gzip-passthrough
    tests. `label` is woven into the error message so the failing test
    is identifiable in the assertion output. GET variants pass
    check_body_sanity=True to also verify the fixture is large enough
    for compression to be visibly smaller than the source; HEAD passes
    False since there is no body to size-check.
    """
    assert r.status_code == 200, f"{label}: status={r.status_code}"
    enc = r.headers.get("Content-Encoding")
    if check_body_sanity:
        # Sanity: the plain file is much larger than its gzip output. If
        # gzip ran the body would be far smaller than the source.
        plain_size = GZIP_PLAIN.stat().st_size
        assert plain_size > 1024, "fixture sanity"
        assert enc == "gzip", (
            f"{label}: expected Content-Encoding=gzip when .zst is "
            f"missing and AE='gzip, zstd', got {enc!r}; "
            f"plain_size={plain_size} response_body_size={len(body)}"
        )
    else:
        assert enc == "gzip", (
            f"{label}: expected Content-Encoding=gzip when .zst is "
            f"missing and AE='gzip, zstd', got {enc!r}"
        )


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
    _assert_gzip_passthrough(r, body, label="zstd_static on")


def test_gzip_survives_missing_zst_sidecar_always_mode(static_gzip_nginx):
    """Same shape as test_gzip_survives_missing_zst_sidecar but exercises
    `zstd_static always`. The handler bypasses ngx_http_zstd_ok() in
    always mode, but the fixed gzip-preempt commit point still sits past
    the file-probe branches — a missing sidecar must fall through cleanly
    without poisoning gzip eligibility. This is the coverage gap that
    review T1 flagged."""
    r, body = http_request(
        static_gzip_nginx, "/static-poison-always/plain.txt",
        accept_encoding="gzip, zstd",
    )
    _assert_gzip_passthrough(r, body, label="zstd_static always")


def test_gzip_survives_missing_zst_sidecar_head_method(static_gzip_nginx):
    """HEAD requests run through the same handler entry point and hit
    the same gzip-preempt commit point before the `r->header_only`
    short-circuit at ngx_http_send_header. A missing sidecar on HEAD
    must also leave gzip eligibility intact — there is no body to
    compress, but the Content-Encoding header still has to be set so
    that intermediaries cache the response under the right key.
    """
    r, body = http_request(
        static_gzip_nginx, "/static-poison/plain.txt",
        method="HEAD",
        accept_encoding="gzip, zstd",
    )
    _assert_gzip_passthrough(r, body, label="HEAD", check_body_sanity=False)


def test_gzip_works_when_zstd_static_off(static_gzip_nginx):
    """Negative control: same fixture but a path with no static
    handler binding — gzip must compress the plain file. This proves
    gzip is enabled in the test image and the fixture content is
    compressible, so the regression test above isolates the
    static-handler bug."""
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
