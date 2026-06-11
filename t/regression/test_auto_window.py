"""Auto-Window: Content-Length-driven cParams tuning.

Task 1 scope: directive registration + bounds validation only. The
per-request auto-tune behaviour (and the matching debug-log emitter
that other tests will grep) lands in Task 2.

Task 2 scope: per-request auto-tune behavioural tests. New tests use
`error_log <path> debug;` to capture the auto-window debug line emitted
by the filter and assert the chosen cParams against expected libzstd
heuristic output for a given Content-Length.

Debug line formats emitted by create_cstream:
  * Auto-tune active:
        zstd auto-window: cl=<N> wlog=<X> hlog=<Y> clog=<Z> ws=<W> baseline=<B>
        (cl=-1 means ZSTD_CONTENTSIZE_UNKNOWN — chunked + operator cap path)
  * Skipped (chunked, no operator cap):
        zstd auto-window: skipped (no content_length_n, no cap), ws=baseline=<B>

The ALERT-level `zstd workspace exhausted` line from the customAlloc
fallback path must remain ABSENT across the matrix (bump-allocator-fit
invariant).
"""

from __future__ import annotations

import os
import re
import socket
import threading
import time
from pathlib import Path
from typing import Callable, Iterator

import pytest
import requests

zstandard = pytest.importorskip("zstandard")

from conftest import (
    BASE_URL,
    _nginx_has_compat,
    http_request,
    nginx_t,
    render_template,
    start_nginx,
    stop_nginx,
    zstd_decompress,
)


# ---------------------------------------------------------------------------
# Task 1 tests (unchanged)
# ---------------------------------------------------------------------------


def test_zstd_window_bits_parses_valid_value():
    """An in-range zstd_window_bits N; survives `nginx -t`, nginx starts
    cleanly, and a simple GET to a zstd-eligible location still produces
    a decompressible zstd response.
    """
    stop_nginx()
    render_template(extra_directives="zstd_window_bits 14;")
    start_nginx()
    try:
        r, body = http_request(BASE_URL, path="/", accept_encoding="zstd")
        assert r.status_code == 200, f"unexpected status: {r.status_code}"
        if r.headers.get("Content-Encoding") == "zstd":
            decoded = zstd_decompress(body)
            assert len(decoded) > 0
    finally:
        stop_nginx()


@pytest.mark.parametrize(
    "label,value,expect_bounds_msg",
    [
        ("below-lower-bound", "5",  True),
        ("above-upper-bound", "99", True),
        ("zero",              "0",  True),
        ("negative",          "-3", False),
    ],
)
def test_zstd_window_bits_rejects_out_of_range(label, value, expect_bounds_msg):
    """`nginx -t` must reject zstd_window_bits values outside the
    libzstd runtime bounds (or syntactically invalid like "-3")."""
    rc, output = nginx_t(extra_directives=f"zstd_window_bits {value};")
    assert rc != 0, (
        f"[{label}] zstd_window_bits {value}; was accepted but should "
        f"have been rejected:\n{output}"
    )
    if expect_bounds_msg:
        assert "zstd_window_bits must be between" in output, (
            f"[{label}] error message missing expected marker:\n{output}"
        )


# ---------------------------------------------------------------------------
# Task 2: per-request auto-tune
# ---------------------------------------------------------------------------

LOG_PATH = Path("/tmp/zstd-auto-window-test.log")
FIXTURE_DIR = Path("/var/fixtures/auto-window")
FIXTURE_PORT = 9005  # distinct from other test fixture ports

# Expected windowLog from libzstd's ZSTD_getCParams(level=6, srcSize=N, 0).
# windowLog = max(ZSTD_WINDOWLOG_MIN, ceil(log2(srcSize))) when srcSize is
# known, but capped at level-default (20 for level=6). We assert
# wlog <= ceil(log2(N)) as the loose invariant (auto-tune never grows the
# window above what the body needs).


def _ceil_log2(n: int) -> int:
    if n <= 1:
        return 1
    return (n - 1).bit_length()


def _compressible_body(size: int) -> bytes:
    """Highly-compressible repeating pattern. Used so the filter's
    zstd_min_length check is trivially satisfied and so compressed-size
    is meaningfully smaller than input (the ratio tests need this)."""
    pattern = b"The quick brown fox jumps over the lazy dog. "
    reps = (size // len(pattern)) + 1
    return (pattern * reps)[:size]


def _near_random_body(size: int) -> bytes:
    """Near-incompressible binary body. Used by the ratio-preservation
    test to exercise the high-entropy branch."""
    return os.urandom(size)


# ---- Upstream TCP fixture -------------------------------------------------
#
# Serves bodies in two modes selected by URL prefix:
#   /known-cl/<size>      : Content-Length response with synthetic body
#   /chunked/<size>       : Transfer-Encoding: chunked (no Content-Length)
#   /near-random-cl/<size>: Content-Length but high-entropy body
#   /near-random-chunked/<size>: chunked + high-entropy body
#   /404-cl               : 404 with Content-Length: 9, body "Not Found"
#   /image/<size>         : 200 with Content-Type: image/png (filter declines)
#
# The handler stores the served body in GROUND_TRUTH[url] under a lock so
# test assertions can byte-compare without races.

GROUND_TRUTH: dict[str, bytes] = {}
GROUND_TRUTH_LOCK = threading.Lock()


def _parse_size_path(path: str, prefix: str) -> int | None:
    if not path.startswith(prefix):
        return None
    tail = path[len(prefix):].split("?", 1)[0].strip("/")
    try:
        return int(tail)
    except ValueError:
        return None


def _send_known_cl(c: socket.socket, size: int, key: str, body_factory) -> None:
    body = body_factory(size)
    with GROUND_TRUTH_LOCK:
        GROUND_TRUTH[key] = body
    c.sendall(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n"
        b"\r\n" + body
    )


def _send_chunked(c: socket.socket, size: int, key: str, body_factory) -> None:
    body = body_factory(size)
    with GROUND_TRUTH_LOCK:
        GROUND_TRUTH[key] = body
    c.sendall(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/plain\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"Connection: close\r\n"
        b"\r\n"
    )
    # One chunk for simplicity — chunked-ness is what matters here, not
    # multi-chunk back-pressure.
    c.sendall(f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")


def _send_empty_cl(c: socket.socket) -> None:
    """200 OK with explicit `Content-Length: 0` and zero body bytes.
    The header filter declines this outright (C12-2) — used to assert
    the decline (no Content-Encoding, no auto-window log line)."""
    with GROUND_TRUTH_LOCK:
        GROUND_TRUTH["/empty-cl"] = b""
    c.sendall(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Length: 0\r\n"
        b"Connection: close\r\n"
        b"\r\n"
    )


def _send_404_cl(c: socket.socket) -> None:
    body = b"Not Found"
    with GROUND_TRUTH_LOCK:
        GROUND_TRUTH["/404-cl"] = body
    c.sendall(
        b"HTTP/1.1 404 Not Found\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Length: 9\r\n"
        b"Connection: close\r\n"
        b"\r\n" + body
    )


def _send_image(c: socket.socket, size: int, key: str) -> None:
    body = os.urandom(size)
    with GROUND_TRUTH_LOCK:
        GROUND_TRUTH[key] = body
    c.sendall(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: image/png\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n"
        b"\r\n" + body
    )


def _handle(c: socket.socket) -> None:
    try:
        c.settimeout(5)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = c.recv(4096)
            if not chunk:
                break
            buf += chunk
        try:
            path = buf.split(b"\r\n", 1)[0].decode("latin-1").split(" ")[1]
        except (IndexError, UnicodeDecodeError):
            path = ""

        n = _parse_size_path(path, "/known-cl/")
        if n is not None:
            _send_known_cl(c, n, path, _compressible_body)
            return
        n = _parse_size_path(path, "/chunked/")
        if n is not None:
            _send_chunked(c, n, path, _compressible_body)
            return
        n = _parse_size_path(path, "/near-random-cl/")
        if n is not None:
            _send_known_cl(c, n, path, _near_random_body)
            return
        n = _parse_size_path(path, "/near-random-chunked/")
        if n is not None:
            _send_chunked(c, n, path, _near_random_body)
            return
        n = _parse_size_path(path, "/image/")
        if n is not None:
            _send_image(c, n, path)
            return
        if path.startswith("/empty-cl"):
            _send_empty_cl(c)
            return
        if path.startswith("/404-cl"):
            _send_404_cl(c)
            return

        c.sendall(
            b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n"
            b"Connection: close\r\n\r\n"
        )
    finally:
        try:
            c.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        c.close()


def _server_loop(sock: socket.socket, stop_event: threading.Event) -> None:
    sock.settimeout(0.5)
    while not stop_event.is_set():
        try:
            c, _ = sock.accept()
        except socket.timeout:
            continue
        threading.Thread(target=_handle, args=(c,), daemon=True).start()


@pytest.fixture(scope="module")
def upstream_fixture() -> Iterator[None]:
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", FIXTURE_PORT))
    sock.listen(16)
    stop = threading.Event()
    thread = threading.Thread(
        target=_server_loop, args=(sock, stop), daemon=True
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2)
        sock.close()


# ---- Helpers --------------------------------------------------------------


def _clear_log() -> None:
    LOG_PATH.unlink(missing_ok=True)


def _read_log() -> str:
    if not LOG_PATH.exists():
        return ""
    return LOG_PATH.read_text()


_ABSENCE_SETTLE_S = 0.5


def _wait_for_log(
    predicate: Callable[[str], bool] | None = None,
    timeout: float = 2.0,
    interval: float = 0.05,
    settle: float | None = None,
) -> str:
    """Poll the error_log file and return its content.

    Replaces the `time.sleep(0.1); log = _read_log()` race-mitigation
    pattern that was duplicated across 10+ test sites. nginx flushes
    error_log asynchronously, so a write made just before the test
    reads the file may not yet be visible.

    Behaviour:
      * With `predicate=None` (default): wait `settle` seconds (default
        _ABSENCE_SETTLE_S = 0.5 s) then return the log. The previous
        implementation hard-capped this at 0.1 s regardless of the
        `timeout` argument, which raced nginx's async error_log flush
        and could miss a line that did get written. 0.5 s is enough to
        cover the worst observed flush latency under the regression
        matrix without inflating absence-test runtime to multi-second
        scale. Pass an explicit `settle=` to override (e.g. when a test
        wants belt-and-suspenders confidence at the cost of a longer
        wait).
      * With a predicate: return as soon as `predicate(log)` is true,
        polling every `interval`. Used for presence assertions to
        keep the suite fast.
    """
    if predicate is None:
        # Absence-style wait: sleep `settle` once, then read. Honor an
        # explicit settle override; otherwise use the module default
        # which is decoupled from the (only-meaningful-for-presence)
        # `timeout` argument.
        time.sleep(_ABSENCE_SETTLE_S if settle is None else settle)
        return _read_log()
    deadline = time.monotonic() + timeout
    log = _read_log()
    while time.monotonic() < deadline:
        if predicate(log):
            return log
        time.sleep(interval)
        log = _read_log()
    return log


AUTO_WINDOW_RE = re.compile(
    r"zstd auto-window: cl=(-?\d+) wlog=(\d+) hlog=(\d+) clog=(\d+) "
    r"ws=(\d+) baseline=(\d+)"
)
SKIPPED_RE = re.compile(
    r"zstd auto-window: skipped \(no content_length_n, no cap\), "
    r"ws=baseline=(\d+)"
)


def _find_auto_window_line(log: str, cl_match: int | None = None) -> re.Match | None:
    """Find the first auto-window debug line in the log, optionally
    filtering by content_length value."""
    for m in AUTO_WINDOW_RE.finditer(log):
        if cl_match is None or int(m.group(1)) == cl_match:
            return m
    return None


PROXY_LOCATIONS = f"""
    location /proxy/ {{
        proxy_pass http://127.0.0.1:{FIXTURE_PORT}/;
        proxy_http_version 1.1;
        proxy_buffering on;
    }}
    location /proxy-cap/ {{
        proxy_pass http://127.0.0.1:{FIXTURE_PORT}/;
        proxy_http_version 1.1;
        proxy_buffering on;
        zstd_window_bits 12;
    }}
    location /proxy-cap14/ {{
        proxy_pass http://127.0.0.1:{FIXTURE_PORT}/;
        proxy_http_version 1.1;
        proxy_buffering on;
        zstd_window_bits 14;
    }}
    location /proxy-chunked-passthrough/ {{
        proxy_pass http://127.0.0.1:{FIXTURE_PORT}/;
        proxy_http_version 1.1;
        proxy_buffering off;
    }}
    location /static/ {{
        alias {FIXTURE_DIR}/;
        default_type text/plain;
    }}
"""


def _start_nginx_with_log(extra_directives_extra: str = "",
                          extra_locations: str = PROXY_LOCATIONS) -> None:
    """Render template with info-level error_log + custom locations.

    The auto-window emitter logs at NGX_LOG_INFO so the regression
    matrix (which uses the non-debug nginx-mainline build) can observe
    the chosen cParams without rebuilding nginx with --with-debug.
    """
    _clear_log()
    directives = (
        f"error_log {LOG_PATH} info;\n"
        + extra_directives_extra
    )
    render_template(
        extra_directives=directives,
        extra_locations=extra_locations,
    )
    start_nginx()


# Body sizes that exercise different windowLog buckets at level=6.
# Level 6 default windowLog is 20 (1 MiB). For srcSize < 1 MiB libzstd
# returns smaller windowLog (ceil(log2(srcSize)) at minimum). At 10 MiB
# it's still capped at the level-default since dictSize=0.
BODY_SIZE_MATRIX = [1024, 65536, 1048576, 10485760]

# Below-floor case: 64 bytes is smaller than libzstd's WINDOWLOG_MIN
# (1 KiB). Verifies the windowLog clamp at 10 (not below).
BELOW_FLOOR_SIZE = 64


# ---------------------------------------------------------------------------
# proxy_pass with known Content-Length
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", BODY_SIZE_MATRIX)
def test_auto_window_proxy_pass_known_content_length_matrix(
    upstream_fixture, size
):
    """proxy_pass to upstream with known Content-Length triggers auto-tune.

    Assertions:
      (a) Content-Encoding: zstd
      (b) decompresses to ground truth
      (c) error_log contains the auto-window line with cl=<size> and
          wlog <= ceil(log2(size))  (loose invariant)
      (d) no `zstd workspace exhausted` (bump-fit)
    """
    stop_nginx()
    _start_nginx_with_log()
    try:
        r = requests.get(
            f"{BASE_URL}/proxy/known-cl/{size}",
            headers={"Accept-Encoding": "zstd"},
            timeout=30,
            stream=True,
        )
        body = r.raw.read(decode_content=False)
        r.close()
        assert r.status_code == 200
        assert r.headers.get("Content-Encoding") == "zstd", (
            f"size={size} did not compress; headers:\n{r.headers}"
        )
        decoded = zstd_decompress(body)
        with GROUND_TRUTH_LOCK:
            truth = GROUND_TRUTH[f"/known-cl/{size}"]
        assert decoded == truth, (
            f"decoded body differs: {len(decoded)} vs {len(truth)}"
        )

        log = _wait_for_log(
            lambda s: _find_auto_window_line(s, cl_match=size) is not None
        )
        m = _find_auto_window_line(log, cl_match=size)
        assert m is not None, (
            f"auto-window line for cl={size} missing from error_log; "
            f"log content:\n{log[-2000:]}"
        )
        wlog = int(m.group(2))
        upper = max(_ceil_log2(size), 10)  # WINDOWLOG_MIN is 10
        assert wlog <= upper, (
            f"size={size}: wlog={wlog} > ceil(log2({size}))={upper}"
        )
        assert "zstd workspace exhausted" not in log, (
            f"workspace fallback fired for size={size}; log:\n{log[-2000:]}"
        )
    finally:
        stop_nginx()


def test_auto_window_below_floor_body_clamps_to_min(upstream_fixture):
    """64-byte body (< libzstd's WINDOWLOG_MIN 1 KiB window). The
    ZSTD_getCParams heuristic must clamp windowLog to its minimum (10);
    the test asserts wlog=10 exactly, guarding against a regression where
    the auto-tune path forgets to enforce libzstd's lower bound."""
    stop_nginx()
    # The test template already sets `zstd_min_length 0;` so 64 bytes
    # is eligible without a directive override.
    _start_nginx_with_log()
    try:
        r = requests.get(
            f"{BASE_URL}/proxy/known-cl/{BELOW_FLOOR_SIZE}",
            headers={"Accept-Encoding": "zstd"},
            timeout=30,
            stream=True,
        )
        body = r.raw.read(decode_content=False)
        r.close()
        assert r.status_code == 200
        assert r.headers.get("Content-Encoding") == "zstd", (
            f"below-floor body should still compress with min_length 1; "
            f"headers:\n{r.headers}"
        )
        decoded = zstd_decompress(body)
        with GROUND_TRUTH_LOCK:
            truth = GROUND_TRUTH[f"/known-cl/{BELOW_FLOOR_SIZE}"]
        assert decoded == truth

        log = _wait_for_log(
            lambda s: _find_auto_window_line(s, cl_match=BELOW_FLOOR_SIZE)
            is not None
        )
        m = _find_auto_window_line(log, cl_match=BELOW_FLOOR_SIZE)
        assert m is not None, (
            f"missing auto-window line for cl={BELOW_FLOOR_SIZE}; "
            f"log tail:\n{log[-2000:]}"
        )
        assert int(m.group(2)) == 10, (
            f"wlog={m.group(2)} expected 10 (WINDOWLOG_MIN) for "
            f"below-floor body cl={BELOW_FLOOR_SIZE}"
        )
        assert "zstd workspace exhausted" not in log
    finally:
        stop_nginx()


def test_auto_window_zero_content_length(upstream_fixture):
    """`Content-Length: 0` 200 OK is DECLINED by the header filter (C12-2),
    so it never reaches the body filter's auto-tune path at all.

    Compressing a known-empty body only emits a pointless empty zstd frame
    and (on the auto-window path) allocates a full baseline workspace for
    zero bytes, so the header filter now declines `content_length_n == 0`
    regardless of min_length. The observable signature is therefore:
      * the response carries NO `Content-Encoding: zstd` and an empty body;
      * NO auto-window line is emitted for cl=0 — create_cstream is never
        entered because the request was declined before the body filter
        (mirrors the HEAD-parity early-return).

    Empirical wlog note (now only of historical interest, since this path is
    declined): libzstd's ZSTD_getCParams_internal special-cases srcSize==0 by
    reassigning it to ZSTD_CONTENTSIZE_UNKNOWN
    (tmp/src/zstd/lib/compress/zstd_compress.c:1633), so cl=0 would yield
    level-default cParams (wlog=19 at level=6), NOT a WINDOWLOG_MIN clamp —
    which is precisely why a known-empty response is declined rather than fed
    through auto-tune.

    Chunked / unknown-length empty responses (content_length_n == -1) are out
    of scope and remain compressed by design — see test_auto_window_chunked_*
    and test_empty_body.py.
    """
    stop_nginx()
    _start_nginx_with_log()
    try:
        r = requests.get(
            f"{BASE_URL}/proxy/empty-cl",
            headers={"Accept-Encoding": "zstd"},
            timeout=30,
            stream=True,
        )
        body = r.raw.read(decode_content=False)
        r.close()
        assert r.status_code == 200, f"status={r.status_code}"
        assert r.headers.get("Content-Encoding") != "zstd", (
            f"known Content-Length: 0 must be declined (C12-2), got "
            f"Content-Encoding={r.headers.get('Content-Encoding')!r}"
        )
        assert body == b"", f"empty body must stay empty, got {len(body)} bytes"

        log = _wait_for_log()
        assert _find_auto_window_line(log, cl_match=0) is None, (
            f"auto-window fired on a known Content-Length: 0 response; it "
            f"should have been declined in the header filter (C12-2) before "
            f"create_cstream. log tail:\n{log[-2000:]}"
        )
    finally:
        stop_nginx()


# ---------------------------------------------------------------------------
# root/static files (nginx core static handler — NOT our zstd_static module)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def static_fixtures(upstream_fixture) -> Iterator[None]:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for size in BODY_SIZE_MATRIX:
        path = FIXTURE_DIR / f"body-{size}"
        path.write_bytes(_compressible_body(size))
    yield


@pytest.mark.parametrize("size", BODY_SIZE_MATRIX)
def test_auto_window_root_static_files_matrix(static_fixtures, size):
    """nginx core's static handler (via `root`) sets Content-Length from
    file size — auto-tune must trigger the same way as on proxy_pass."""
    stop_nginx()
    _start_nginx_with_log()
    try:
        r = requests.get(
            f"{BASE_URL}/static/body-{size}",
            headers={"Accept-Encoding": "zstd"},
            timeout=30,
            stream=True,
        )
        body = r.raw.read(decode_content=False)
        r.close()
        assert r.status_code == 200
        assert r.headers.get("Content-Encoding") == "zstd", (
            f"size={size} static file not compressed"
        )
        decoded = zstd_decompress(body)
        expected = _compressible_body(size)
        assert decoded == expected

        log = _wait_for_log(
            lambda s: _find_auto_window_line(s, cl_match=size) is not None
        )
        m = _find_auto_window_line(log, cl_match=size)
        assert m is not None, (
            f"auto-window line for cl={size} missing; log tail:\n{log[-2000:]}"
        )
        wlog = int(m.group(2))
        upper = max(_ceil_log2(size), 10)
        assert wlog <= upper
        assert "zstd workspace exhausted" not in log
    finally:
        stop_nginx()


# ---------------------------------------------------------------------------
# chunked, no operator cap → auto-tune skipped
# ---------------------------------------------------------------------------


def test_auto_window_chunked_skipped(upstream_fixture):
    """Chunked upstream (no Content-Length), no zstd_window_bits → the
    auto-tune path is skipped and the workspace is the level baseline.
    """
    stop_nginx()
    _start_nginx_with_log()
    try:
        r = requests.get(
            f"{BASE_URL}/proxy-chunked-passthrough/chunked/65536",
            headers={"Accept-Encoding": "zstd"},
            timeout=30,
            stream=True,
        )
        body = r.raw.read(decode_content=False)
        r.close()
        assert r.status_code == 200
        assert r.headers.get("Content-Encoding") == "zstd"
        decoded = zstd_decompress(body)
        with GROUND_TRUTH_LOCK:
            truth = GROUND_TRUTH["/chunked/65536"]
        assert decoded == truth

        log = _wait_for_log(lambda s: SKIPPED_RE.search(s) is not None)
        assert SKIPPED_RE.search(log), (
            f"missing 'skipped' debug line for chunked path; log tail:\n"
            f"{log[-2000:]}"
        )
        assert "zstd workspace exhausted" not in log
    finally:
        stop_nginx()


# ---------------------------------------------------------------------------
# chunked + operator cap → auto-tune path activates with cl=-1
# ---------------------------------------------------------------------------


def test_auto_window_chunked_with_cap_applies(upstream_fixture):
    """Chunked upstream + zstd_window_bits 14; on the location triggers
    auto-tune uniformly (cl=-1 marker)."""
    stop_nginx()
    # Use a location that combines proxy_buffering off (chunked-through)
    # with a window cap.
    locations = PROXY_LOCATIONS + f"""
    location /proxy-chunked-cap/ {{
        proxy_pass http://127.0.0.1:{FIXTURE_PORT}/;
        proxy_http_version 1.1;
        proxy_buffering off;
        zstd_window_bits 14;
    }}
"""
    _start_nginx_with_log(extra_locations=locations)
    try:
        r = requests.get(
            f"{BASE_URL}/proxy-chunked-cap/chunked/65536",
            headers={"Accept-Encoding": "zstd"},
            timeout=30,
            stream=True,
        )
        body = r.raw.read(decode_content=False)
        r.close()
        assert r.status_code == 200
        assert r.headers.get("Content-Encoding") == "zstd"
        decoded = zstd_decompress(body)
        with GROUND_TRUTH_LOCK:
            truth = GROUND_TRUTH["/chunked/65536"]
        assert decoded == truth

        log = _wait_for_log(
            lambda s: _find_auto_window_line(s, cl_match=-1) is not None
        )
        m = _find_auto_window_line(log, cl_match=-1)
        assert m is not None, (
            f"missing auto-window cl=-1 line; log tail:\n{log[-2000:]}"
        )
        assert int(m.group(2)) == 14, (
            f"wlog={m.group(2)} expected 14 (cap)"
        )
        assert "zstd workspace exhausted" not in log
    finally:
        stop_nginx()


# ---------------------------------------------------------------------------
# baseline isolated per loc_conf (different levels → different ws=baseline)
# ---------------------------------------------------------------------------


def test_auto_window_baseline_isolated_per_loc_conf(upstream_fixture):
    """Two locations with distinct zstd_comp_level — chunked requests
    (skipped path) must report distinct ws=baseline values."""
    stop_nginx()
    locations = f"""
    location /proxy-l1/ {{
        proxy_pass http://127.0.0.1:{FIXTURE_PORT}/;
        proxy_http_version 1.1;
        proxy_buffering off;
        zstd_comp_level 1;
    }}
    location /proxy-l6/ {{
        proxy_pass http://127.0.0.1:{FIXTURE_PORT}/;
        proxy_http_version 1.1;
        proxy_buffering off;
        zstd_comp_level 6;
    }}
"""
    _start_nginx_with_log(extra_locations=locations)
    try:
        for prefix in ("/proxy-l1", "/proxy-l6"):
            r = requests.get(
                f"{BASE_URL}{prefix}/chunked/8192",
                headers={"Accept-Encoding": "zstd"},
                timeout=15,
                stream=True,
            )
            r.raw.read(decode_content=False)
            r.close()
            assert r.status_code == 200
        log = _wait_for_log(
            lambda s: len(SKIPPED_RE.findall(s)) >= 2
        )
        baselines = [int(m.group(1)) for m in SKIPPED_RE.finditer(log)]
        assert len(baselines) >= 2, (
            f"expected at least 2 skipped lines, got {len(baselines)}; "
            f"log tail:\n{log[-2000:]}"
        )
        # level=1 baseline is strictly smaller than level=6 baseline.
        # Assert that at least two distinct baselines are present.
        unique = set(baselines)
        assert len(unique) >= 2, (
            f"expected distinct baselines per loc_conf, all values equal: "
            f"{baselines}"
        )
    finally:
        stop_nginx()


# ---------------------------------------------------------------------------
# directive cap applied (known-CL + cap=12)
# ---------------------------------------------------------------------------


def test_auto_window_directive_cap_applied(upstream_fixture):
    """zstd_window_bits 12; on a location forces wlog=12 even when the
    body is 1 MiB (auto-derived wlog would be ~20)."""
    stop_nginx()
    _start_nginx_with_log()
    try:
        size = 1048576
        r = requests.get(
            f"{BASE_URL}/proxy-cap/known-cl/{size}",
            headers={"Accept-Encoding": "zstd"},
            timeout=30,
            stream=True,
        )
        body = r.raw.read(decode_content=False)
        r.close()
        assert r.status_code == 200
        assert r.headers.get("Content-Encoding") == "zstd"
        decoded = zstd_decompress(body)
        with GROUND_TRUTH_LOCK:
            truth = GROUND_TRUTH[f"/known-cl/{size}"]
        assert decoded == truth

        log = _wait_for_log(
            lambda s: _find_auto_window_line(s, cl_match=size) is not None
        )
        m = _find_auto_window_line(log, cl_match=size)
        assert m is not None, (
            f"missing auto-window line; log tail:\n{log[-2000:]}"
        )
        assert int(m.group(2)) == 12, (
            f"wlog={m.group(2)}, expected 12 (cap)"
        )
        assert "zstd workspace exhausted" not in log
    finally:
        stop_nginx()


# ---------------------------------------------------------------------------
# HEAD request parity
# ---------------------------------------------------------------------------


def test_auto_window_head_request_parity(upstream_fixture):
    """HEAD through proxy_pass with zstd on — header filter sets the
    Content-Encoding response header but the body filter early-returns
    (header_only). create_cstream must not be invoked, so no auto-window
    line, no workspace fallback, no crash."""
    stop_nginx()
    _start_nginx_with_log()
    try:
        r = requests.head(
            f"{BASE_URL}/proxy/known-cl/16384",
            headers={"Accept-Encoding": "zstd"},
            timeout=15,
        )
        assert r.status_code == 200
        # HEAD body must be empty regardless of Content-Encoding header.
        assert r.content == b""
        # Positive assertion: header filter still negotiated zstd for the
        # HEAD response. A regression that declined zstd entirely for HEAD
        # would also satisfy the "no auto-window line" check below.
        assert r.headers.get("Content-Encoding") == "zstd", (
            f"expected Content-Encoding: zstd on HEAD response, "
            f"got headers={dict(r.headers)}"
        )

        log = _wait_for_log()
        # No fallback alert and no crash trace.
        assert "zstd workspace exhausted" not in log
        # HEAD requests early-return in header_filter (r->header_only),
        # so create_cstream is never entered → no auto-window log line
        # at all. (Other tests' requests don't touch this nginx instance
        # because each test stops/starts nginx.)
        assert _find_auto_window_line(log) is None, (
            f"auto-window fired on HEAD request (should have early-returned "
            f"in header_filter); log tail:\n{log[-2000:]}"
        )
        assert "auto-window: skipped" not in log, (
            f"auto-window emitted skipped marker on HEAD; log tail:\n"
            f"{log[-2000:]}"
        )
    finally:
        stop_nginx()


# ---------------------------------------------------------------------------
# 404 with Content-Length (filter still compresses)
# ---------------------------------------------------------------------------


def test_auto_window_error_response_with_content_length(upstream_fixture):
    """Upstream returns 404 + Content-Length: 9. With `zstd_min_length 1;`
    the filter compresses the tiny body (header_filter accepts 404).
    Tiny C-L drives windowLog to libzstd's minimum (10), so we assert
    the exact log line shape with cl=9 wlog=10."""
    stop_nginx()
    # The test template already sets `zstd_min_length 0;` so the 9-byte
    # body is eligible without a directive override.
    _start_nginx_with_log()
    try:
        r = requests.get(
            f"{BASE_URL}/proxy/404-cl",
            headers={"Accept-Encoding": "zstd"},
            timeout=15,
            stream=True,
        )
        body = r.raw.read(decode_content=False)
        r.close()
        assert r.status_code == 404
        assert r.headers.get("Content-Encoding") == "zstd", (
            f"expected zstd-compressed 404 body; headers:\n{r.headers}"
        )
        decoded = zstd_decompress(body)
        assert decoded == b"Not Found", f"decoded={decoded!r}"

        log = _wait_for_log(
            lambda s: _find_auto_window_line(s, cl_match=9) is not None
        )
        m = _find_auto_window_line(log, cl_match=9)
        assert m is not None, (
            f"missing auto-window line for cl=9; log tail:\n{log[-2000:]}"
        )
        # libzstd's ZSTD_WINDOWLOG_MIN is 10; auto-tune cannot go below.
        assert int(m.group(2)) == 10, (
            f"wlog={m.group(2)} expected 10 (WINDOWLOG_MIN) for cl=9"
        )
        assert "zstd workspace exhausted" not in log
    finally:
        stop_nginx()


# ---------------------------------------------------------------------------
# Content-Type rejected by filter → auto-window path never reached
# ---------------------------------------------------------------------------


def test_auto_window_content_type_rejected_skipped(upstream_fixture):
    """image/png is NOT in the default zstd_types — filter declines at
    the header_filter ngx_http_test_content_type check, BEFORE the body
    filter ever runs create_cstream. Therefore NO auto-window line at all
    and the body is pass-through identical to upstream."""
    stop_nginx()
    # Restrict zstd_types to text only so image/* is unambiguously
    # rejected (the template's default is `zstd_types *;` which would
    # match everything, defeating the test). Override at the location
    # scope to keep other tests' http-context defaults intact.
    locations = f"""
    location /image-type-rejected/ {{
        proxy_pass http://127.0.0.1:{FIXTURE_PORT}/;
        proxy_http_version 1.1;
        proxy_buffering on;
        zstd_types text/plain;
    }}
"""
    _start_nginx_with_log(extra_locations=locations)
    try:
        size = 4096
        r = requests.get(
            f"{BASE_URL}/image-type-rejected/image/{size}",
            headers={"Accept-Encoding": "zstd"},
            timeout=15,
            stream=True,
        )
        body = r.raw.read(decode_content=False)
        r.close()
        assert r.status_code == 200
        assert r.headers.get("Content-Encoding") != "zstd"
        with GROUND_TRUTH_LOCK:
            truth = GROUND_TRUTH[f"/image/{size}"]
        assert body == truth, "image body should pass through unmodified"

        log = _wait_for_log()
        # Auto-window must NOT have fired for this content-type-rejected
        # request: the filter declines at ngx_http_test_content_type
        # (header filter) so create_cstream is never entered. Since this
        # is the only request against this fresh nginx instance, the
        # cl=<size> line must be absent.
        assert _find_auto_window_line(log, cl_match=size) is None, (
            f"auto-window fired for content-type-rejected request "
            f"(should have skipped at header filter); log tail:\n"
            f"{log[-2000:]}"
        )
        # The skipped-no-cl line must also be absent (no compression at all).
        assert "auto-window: skipped" not in log, (
            f"auto-window emitted skipped marker even though filter "
            f"declined at content-type check; log tail:\n{log[-2000:]}"
        )
        assert "zstd workspace exhausted" not in log
    finally:
        stop_nginx()


# ---------------------------------------------------------------------------
# dict interaction
# ---------------------------------------------------------------------------


_DICT_FIXTURE_PATH = Path("/var/fixtures/auto-window-dict.raw")


def test_auto_window_dict_interaction(upstream_fixture):
    """zstd_dict_file is mutually exclusive with the auto-tune cParams
    setParameter sequence: applying windowLog/hashLog/chainLog AFTER
    refCDict would override the CDict's baked cParams and risk both
    ratio regressions and mis-sized workspace estimates (the estimate
    doesn't account for dict-load overhead).

    The implementation short-circuits create_cstream's auto-tune branch
    when zlcf->dict != NULL and emits a `skipped (dict configured)`
    info-line instead. This test verifies:
      (a) compression still works end-to-end with a dict configured
      (b) the auto-tune cl=<size> line is ABSENT
      (c) the skipped-dict marker is PRESENT
      (d) no workspace-exhausted ALERT
    """
    _DICT_FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Arbitrary bytes are valid as a raw dict (see test_dict_reload.py).
    # libzstd's ZSTD_createCDict_byReference accepts any non-empty buffer.
    _DICT_FIXTURE_PATH.write_bytes(b"D" * 4096)

    stop_nginx()
    _start_nginx_with_log(
        extra_directives_extra=f"zstd_dict_file {_DICT_FIXTURE_PATH};\n",
    )
    try:
        size = 16384
        r = requests.get(
            f"{BASE_URL}/proxy/known-cl/{size}",
            headers={"Accept-Encoding": "zstd"},
            timeout=30,
            stream=True,
        )
        body = r.raw.read(decode_content=False)
        r.close()
        assert r.status_code == 200
        assert r.headers.get("Content-Encoding") == "zstd"
        # Body decompresses with the same dict. python-zstandard's
        # ZstdDecompressor takes a dict via dict_data; conftest's
        # zstd_decompress doesn't accept a dict so we use the raw lib.
        dict_data = zstandard.ZstdCompressionDict(_DICT_FIXTURE_PATH.read_bytes())
        dctx = zstandard.ZstdDecompressor(dict_data=dict_data)
        # The wire frame omits content-size (nginx streams via
        # ZSTD_compressStream2 without pledged srcSize), so use the
        # streaming decompressor rather than dctx.decompress() which
        # requires it.
        decoded = dctx.decompressobj().decompress(body)
        with GROUND_TRUTH_LOCK:
            truth = GROUND_TRUTH[f"/known-cl/{size}"]
        assert decoded == truth, "dict-based decompression mismatch"

        log = _wait_for_log(
            lambda s: "zstd auto-window: skipped (dict configured)" in s
        )
        # Auto-tune must be SKIPPED (no cl=<size> line for this request).
        assert _find_auto_window_line(log, cl_match=size) is None, (
            f"auto-tune fired despite dict configured; log tail:\n"
            f"{log[-2000:]}"
        )
        # The dict-specific skipped marker MUST be present.
        assert "zstd auto-window: skipped (dict configured)" in log, (
            f"missing dict-skipped marker; log tail:\n{log[-2000:]}"
        )
        assert "zstd workspace exhausted" not in log
    finally:
        stop_nginx()


# ---------------------------------------------------------------------------
# ratio preservation (no operator cap)
# ---------------------------------------------------------------------------


RATIO_LOG_PATH = Path("/tmp/zstd-auto-window-ratio.log")


def _ratio_locations(extra_directive: str = "") -> str:
    """Locations that record $zstd_ratio via access_log.

    Per nginx semantics $zstd_ratio is only populated AFTER the body
    filter completes — too late for add_header in the header filter
    pipeline. We mirror test_zstd_ratio.py's approach: log the variable
    via access_log and tail the file after the request returns. The
    log_format is defined in http-context via extra_directives.

    Three locations exposed:
      /ratio-known/      — known-CL, no cap → auto-tune picks the snug
                           windowLog for the body
      /ratio-known-tinywin/ — known-CL, zstd_window_bits 10; forces the
                           smallest legal windowLog (1 KiB window) so
                           any body larger than 1 KiB loses
                           back-references → ratio degrades. Used as
                           the apples-to-apples baseline against which
                           auto-tune must NOT regress
      /ratio-chunked/    — chunked + proxy_buffering off; subject to
                           per-chunk flush boundaries that inflate
                           bytes_out independently of windowLog
                           (informational only)
    """
    return f"""
    access_log {RATIO_LOG_PATH} zstd_ratio_fmt;

    location /ratio-known/ {{
        proxy_pass http://127.0.0.1:{FIXTURE_PORT}/;
        proxy_http_version 1.1;
        proxy_buffering on;
        {extra_directive}
    }}
    location /ratio-known-tinywin/ {{
        proxy_pass http://127.0.0.1:{FIXTURE_PORT}/;
        proxy_http_version 1.1;
        proxy_buffering on;
        zstd_window_bits 10;
    }}
    location /ratio-chunked/ {{
        proxy_pass http://127.0.0.1:{FIXTURE_PORT}/;
        proxy_http_version 1.1;
        proxy_buffering off;
        {extra_directive}
    }}
"""


def _ratio_log_format() -> str:
    return (
        'log_format zstd_ratio_fmt "$request_uri ratio=$zstd_ratio";\n'
    )


def _ratio_for_uri(uri: str, timeout_s: float = 3.0) -> float | None:
    """Tail the ratio log and return the float ratio for a given URI,
    or None if the URI hasn't been logged or ratio is '-'."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if RATIO_LOG_PATH.exists():
            for line in RATIO_LOG_PATH.read_text().splitlines():
                if line.startswith(uri + " "):
                    m = re.search(r"ratio=([\d.]+)", line)
                    if m:
                        try:
                            return float(m.group(1))
                        except ValueError:
                            pass
                    return None
        time.sleep(0.1)
    return None


@pytest.mark.parametrize("size", [65536, 1048576])
def test_auto_window_ratio_preserved_no_cap(upstream_fixture, size):
    """Apples-to-apples ratio discriminator: both routes are known-CL +
    proxy_buffering on (same flush shape; eliminates the chunked-flush
    confound from earlier iterations). The only difference is window:

      /ratio-known/         — no cap, auto-tune picks wlog≈ceil(log2(size))
      /ratio-known-tinywin/ — zstd_window_bits 10; (1 KiB window)

    For body sizes ≥ 64 KiB the tiny window strictly loses
    back-references that the auto-tuned path can still reach. The
    invariant `ratio_auto > ratio_tiny` is robust: a future regression
    that broke auto-tune (e.g. silently capped wlog to 10) would make
    the two ratios converge and trip this assertion. A regression in
    the opposite direction (auto-tune somehow producing worse ratio
    than wlog=10) would also fail.

    A strict ratio degradation of at least 5% is required: well above
    measurement noise yet well below the actual delta empirically
    observed at 64 KiB+ (typically 2-10x).
    """
    stop_nginx()
    RATIO_LOG_PATH.unlink(missing_ok=True)
    _start_nginx_with_log(
        extra_directives_extra=_ratio_log_format(),
        extra_locations=_ratio_locations(),
    )
    try:
        auto_path = f"/ratio-known/known-cl/{size}"
        tiny_path = f"/ratio-known-tinywin/known-cl/{size}"

        def _fetch(path: str) -> requests.Response:
            rr = requests.get(
                BASE_URL + path,
                headers={"Accept-Encoding": "zstd"},
                timeout=30,
                stream=True,
            )
            rr.raw.read(decode_content=False)
            rr.close()
            return rr

        r_auto = _fetch(auto_path)
        r_tiny = _fetch(tiny_path)
        assert r_auto.headers.get("Content-Encoding") == "zstd"
        assert r_tiny.headers.get("Content-Encoding") == "zstd"

        ratio_auto = _ratio_for_uri(auto_path)
        ratio_tiny = _ratio_for_uri(tiny_path)
        assert ratio_auto is not None and ratio_auto > 0, (
            f"ratio_auto unavailable for {auto_path}; log:\n"
            f"{RATIO_LOG_PATH.read_text() if RATIO_LOG_PATH.exists() else '<no log>'}"
        )
        assert ratio_tiny is not None and ratio_tiny > 0, (
            f"ratio_tiny unavailable for {tiny_path}"
        )

        # Auto-tune must strictly outperform the wlog=10 cap on a body
        # large enough to span multiple tiny-windows. 5% headroom is
        # deliberately loose vs the empirically observed multi-X delta.
        assert ratio_auto > ratio_tiny * 1.05, (
            f"size={size}: auto-tune did not outperform wlog=10 cap "
            f"(ratio_auto={ratio_auto:.3f} not > ratio_tiny={ratio_tiny:.3f} "
            f"* 1.05). Auto-window may have regressed — verify auto-tune "
            f"still fires per the per-request `auto-window: cl=` log line."
        )

        log = _read_log()
        assert "zstd workspace exhausted" not in log
    finally:
        stop_nginx()


# ---------------------------------------------------------------------------
# cap → ratio trade-off (monotonic non-increasing)
# ---------------------------------------------------------------------------


def test_auto_window_directive_cap_ratio_tradeoff(upstream_fixture):
    """1 MiB compressible body. Sweep zstd_window_bits over a sequence
    of caps and assert the ratio is monotonic non-decreasing in cap size
    (wider window → at least as good a ratio). Each cap runs in a fresh
    nginx instance so the directive applies in isolation; $zstd_ratio is
    captured via access_log (variable populates after the body filter).

    Sequence: [14, 17, 20, None]. wlog=14 (16 KiB window) is the tightest
    constraint that still produces meaningful compression for 1 MiB of
    highly-repetitive text; wlog=20 is the level=6 default; None lets
    auto-tune pick (which yields wlog=20 at this body size since
    ZSTD_getCParams returns level-default for srcSize ≤ 1 MiB).
    """
    size = 1048576
    cap_sequence = [14, 17, 20, None]
    ratios: dict[object, float] = {}
    for cap in cap_sequence:
        stop_nginx()
        RATIO_LOG_PATH.unlink(missing_ok=True)
        extra = f"zstd_window_bits {cap};" if cap is not None else ""
        _start_nginx_with_log(
            extra_directives_extra=_ratio_log_format(),
            extra_locations=_ratio_locations(extra),
        )
        try:
            uri = f"/ratio-known/known-cl/{size}"
            r = requests.get(
                BASE_URL + uri,
                headers={"Accept-Encoding": "zstd"},
                timeout=30,
                stream=True,
            )
            body = r.raw.read(decode_content=False)
            r.close()
            assert r.status_code == 200
            assert r.headers.get("Content-Encoding") == "zstd"
            decoded = zstd_decompress(body)
            with GROUND_TRUTH_LOCK:
                truth = GROUND_TRUTH[f"/known-cl/{size}"]
            assert decoded == truth, "body round-trip failed"

            ratio = _ratio_for_uri(uri)
            assert ratio is not None and ratio > 0, (
                f"ratio missing in access_log; cap={cap}; log:\n"
                f"{RATIO_LOG_PATH.read_text() if RATIO_LOG_PATH.exists() else '<no log>'}"
            )
            ratios[cap] = ratio
            print(f"\n[cap={cap}] ratio={ratio:.3f}")

            log = _read_log()
            assert "zstd workspace exhausted" not in log
        finally:
            stop_nginx()

    # Monotonic non-decreasing in cap size: 14 ≤ 17 ≤ 20 ≤ None
    # (None ≡ no cap ≡ auto-tune; for srcSize ≤ 1 MiB it yields wlog=20
    # which matches the explicit cap=20 case → equality is allowed).
    assert ratios[14] <= ratios[17] + 1e-6, (
        f"ratio not monotonic: cap=14 {ratios[14]:.3f} > cap=17 "
        f"{ratios[17]:.3f}"
    )
    assert ratios[17] <= ratios[20] + 1e-6, (
        f"ratio not monotonic: cap=17 {ratios[17]:.3f} > cap=20 "
        f"{ratios[20]:.3f}"
    )
    assert ratios[20] <= ratios[None] + 1e-6, (
        f"ratio not monotonic: cap=20 {ratios[20]:.3f} > no-cap "
        f"{ratios[None]:.3f}"
    )


# Forward-compat fallback (code review only — see ngx_http_zstd_filter_module.c
# create_cstream "Forward-compat guard" comment block). No runtime test:
# triggering the path requires injecting a fault into libzstd's
# ZSTD_estimateCStreamSize_usingCParams to make it return a value greater
# than zlcf->baseline_ws, which is not feasible without scaffolding that's
# not worth its surface area.
