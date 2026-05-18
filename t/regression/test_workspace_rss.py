"""Direction A — early-release workspace regression tests.

Background:
- filter/ngx_http_zstd_filter_module.c historically pins the CStream
  workspace (~530 KB on L1, ~5.5 MB on L6) to r->pool->large for the
  entire request lifetime, deferring release until pool teardown.
- Direction A mirrors nginx gzip filter's bump allocator + early ngx_pfree
  pattern (tmp/src/nginx/src/http/modules/ngx_http_gzip_filter_module.c
  lines 47-49, 615, 893, 925-971), releasing workspace bytes the moment
  ctx->done flips inside compress().

Design source: docs/zstd-memory-optimization-2026-05-14.md Direction A section.

Why no strict-xfail TDD anchor (empirical):
Three iterations of attempted RSS-based anchors on stable HEAD produced
inconsistent deltas dominated by glibc's mmap heuristics:

- 1st compression on a fresh worker: ΔRSS ≈ 420 KiB (lazy page-in,
  many libzstd workspace pages never faulted in)
- 2nd compression: ΔRSS ≈ 5400 KiB (glibc mmap'd a fresh region, full
  workspace pages resident)
- 3rd and subsequent: ΔRSS ≈ 0 KiB (region from request 2 is reused;
  pages already resident, no new heap growth)

After Direction A the slow-client window would munmap the workspace chunk,
but the visible RSS effect depends on whether glibc had mmap'd the chunk
in the first place — a state-dependent decision. A strict assertion is
therefore not robust in either direction on a single-request scenario.

Direction A's correctness gates are owned by the existing harness:
- ASan harness (t/asan.sh) — freeCStream-vs-pfree ordering UAF
- Valgrind harness (t/valgrind.sh) — abort-path leak (cleanup handler)
- test_workspace_no_fallback (this module) — ZSTD_estimateCStreamSize budget

Its memory effect is captured informationally in
test_workspace_memory_observed (this module) — run with `pytest -s` to
emit the before/after numbers for the commit message.

The win shape Direction A targets is observable in production traffic
profiles where many concurrent requests hold workspace simultaneously
(ab -c 1000 small-response scenario per docs/zstd-memory-optimization
section 'ab -c 1000 keepalive'). Single-request isolated RSS measurement
is dominated by glibc heuristics, not by the fix itself.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path

import pytest
import requests

from conftest import (
    BASE_URL,
    PID_PATH,
    PORT,
    nginx_worker_pid_from_master,
    render_template,
    start_nginx,
    stop_nginx,
)

WORKSPACE_FIXTURE_DIR = Path("/var/fixtures/workspace")
COMPRESSIBLE_BODY_SIZE = 1 * 1024 * 1024  # 1 MB
COMPRESSIBLE_BODY_NAME = "compressible-1mb.bin"


def _master_pid() -> int:
    return int(PID_PATH.read_text().strip())


def _effective_worker_pid() -> int:
    """Worker PID, falling back to master in single-process foreground
    mode (ZSTD_REGRESSION_NO_DAEMON=1, used by t/asan.sh / t/valgrind.sh).
    """
    master = _master_pid()
    worker = nginx_worker_pid_from_master(master)
    return worker if worker is not None else master


def _proc_status_kib(pid: int, field: str) -> int:
    """Read a VmXxx field from /proc/<pid>/status. Returns 0 on miss."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith(f"{field}:"):
                return int(line.split()[1])
    except FileNotFoundError:
        pass
    return 0


def _ensure_compressible_body() -> Path:
    """Write a highly compressible 1 MB body fixture once per session."""
    WORKSPACE_FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    body = WORKSPACE_FIXTURE_DIR / COMPRESSIBLE_BODY_NAME
    if not body.exists() or body.stat().st_size != COMPRESSIBLE_BODY_SIZE:
        chunk = (
            b"The quick brown fox jumps over the lazy dog. "
            b"Sphinx of black quartz, judge my vow.\n"
        )
        n = (COMPRESSIBLE_BODY_SIZE + len(chunk) - 1) // len(chunk)
        body.write_bytes((chunk * n)[:COMPRESSIBLE_BODY_SIZE])
    return body


@pytest.fixture(scope="module")
def workspace_nginx():
    """Module-scoped nginx with zstd_comp_level 6 (largest workspace in
    the default test matrix configuration — ~5.5 MB per ZSTD_estimateCStreamSize)
    and a /workspace/ location aliased to a 1 MB compressible fixture body."""
    _ensure_compressible_body()
    stop_nginx()
    render_template(
        extra_directives="zstd_comp_level 6;",
        extra_locations=(
            "location /workspace/ {\n"
            f"    alias {WORKSPACE_FIXTURE_DIR}/;\n"
            "    default_type application/octet-stream;\n"
            "}\n"
        ),
    )
    start_nginx()
    try:
        yield BASE_URL
    finally:
        stop_nginx()


def _warmup_compression(url_path: str) -> None:
    """One compression request fully drained — touches libzstd's working
    set + grows glibc's heap pool. Subsequent compression-RSS samples
    are then comparable across runs without first-request lazy-alloc noise."""
    r = requests.get(
        f"{BASE_URL}{url_path}",
        headers={"Accept-Encoding": "zstd"},
        timeout=10,
        stream=True,
    )
    _ = r.raw.read(decode_content=False)
    r.close()


def test_workspace_compression_smoke(workspace_nginx):
    """Smoke: GET the 1 MB compressible fixture, assert zstd encoding +
    decompressed length matches input. This is the existence-of-codepath
    test for the /workspace/ location used by the other observers."""
    r = requests.get(
        f"{BASE_URL}/workspace/{COMPRESSIBLE_BODY_NAME}",
        headers={"Accept-Encoding": "zstd"},
        timeout=10,
        stream=True,
    )
    r.raise_for_status()
    assert r.headers.get("Content-Encoding") == "zstd", r.headers
    body = r.raw.read(decode_content=False)
    r.close()
    from conftest import zstd_decompress
    decoded = zstd_decompress(body)
    assert len(decoded) == COMPRESSIBLE_BODY_SIZE, (
        f"decoded length mismatch: got {len(decoded)} bytes, "
        f"expected {COMPRESSIBLE_BODY_SIZE}"
    )




def test_workspace_memory_observed(workspace_nginx):
    """Informational: capture worker memory metrics across the request
    lifecycle for both fast-drain and slow-client paths.

    NOT an anchor — no strict assertion on deltas. Used to produce
    numerical evidence in the Direction A commit message body. Run with
    `pytest -s` to see the printed table.

    Three checkpoints:
      T0  pre-request baseline
      T1  during slow-client window (300 ms after sending GET, before
          draining body — compression has completed inside nginx, ctx->done
          has flipped, but request_pool is still alive)
      T2  post fast-drain (separate request, fully read, pool destroyed)

    Pre-fix (current stable HEAD): T1 - T0 holds workspace bytes in
    r->pool->large until pool destroy. T2 - T0 reflects whatever glibc
    keeps from heap-pooled small allocations.

    Post-fix (Direction A): T1 - T0 should be smaller — the bump allocator's
    one large preallocated chunk has been ngx_pfree'd, returning its mmap'd
    pages to the kernel. T2 - T0 should also be smaller if the chunk was
    >128 KB (always true: workspace > 530 KB at L1 and up).
    """
    worker_pid = _effective_worker_pid()

    rss_t0 = _proc_status_kib(worker_pid, "VmRSS")
    vsz_t0 = _proc_status_kib(worker_pid, "VmSize")

    # Slow-client checkpoint (T1)
    with socket.create_connection(("127.0.0.1", PORT), timeout=10) as sock:
        sock.sendall(
            f"GET /workspace/{COMPRESSIBLE_BODY_NAME} HTTP/1.1\r\n"
            f"Host: 127.0.0.1\r\n"
            f"Accept-Encoding: zstd\r\n"
            f"Connection: close\r\n"
            f"\r\n".encode()
        )
        sock.settimeout(5.0)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        time.sleep(0.3)
        rss_t1 = _proc_status_kib(worker_pid, "VmRSS")
        vsz_t1 = _proc_status_kib(worker_pid, "VmSize")

    # Fast-drain checkpoint (T2)
    r = requests.get(
        f"{BASE_URL}/workspace/{COMPRESSIBLE_BODY_NAME}",
        headers={"Accept-Encoding": "zstd"},
        timeout=10,
        stream=True,
    )
    _ = r.raw.read(decode_content=False)
    r.close()
    time.sleep(0.1)
    rss_t2 = _proc_status_kib(worker_pid, "VmRSS")
    vsz_t2 = _proc_status_kib(worker_pid, "VmSize")

    print(
        "\n[workspace] memory metrics (KiB):\n"
        f"  T0 (baseline)         VmRSS={rss_t0:>7}  VmSize={vsz_t0:>7}\n"
        f"  T1 (slow-client mid)  VmRSS={rss_t1:>7}  VmSize={vsz_t1:>7}  "
        f"ΔRSS={rss_t1 - rss_t0:>+7}  ΔVSZ={vsz_t1 - vsz_t0:>+7}\n"
        f"  T2 (post fast-drain)  VmRSS={rss_t2:>7}  VmSize={vsz_t2:>7}  "
        f"ΔRSS={rss_t2 - rss_t0:>+7}  ΔVSZ={vsz_t2 - vsz_t0:>+7}\n"
    )

    # Sanity only — samples succeeded, request hit the compression path.
    assert rss_t0 > 0 and rss_t1 > 0 and rss_t2 > 0, (
        f"VmRSS sampling failed: T0={rss_t0}, T1={rss_t1}, T2={rss_t2}"
    )


@pytest.mark.parametrize("level", [1, 3, 6, 9])
def test_workspace_no_fallback(level):
    """Sanity: ZSTD_estimateCStreamSize(level) must cover libzstd's actual
    internal allocation needs.

    On stable HEAD (no Direction A) the fallback warning code path does
    not exist, so this test is trivially GREEN. After Direction A lands,
    this test catches regressions where a future libzstd version's
    workspace requirement exceeds the estimate — which would force the
    fallback `ngx_palloc(r->pool, size)` path and emit the warning.

    Uses an http-context error_log directive to capture WARN+ events to a
    file we can grep, since the default template logs to /dev/stderr.

    This test manages its own nginx lifecycle (stops + restarts per
    parametrize) and is declared LAST in the module so any pytest
    scheduling that runs it after workspace_nginx-using tests doesn't
    leave the module-scoped fixture's nginx in a stopped state — module
    teardown then idempotently re-stops.
    """
    log_path = Path("/tmp/zstd-workspace-test.log")
    log_path.unlink(missing_ok=True)

    stop_nginx()
    _ensure_compressible_body()
    render_template(
        extra_directives=(
            f"error_log {log_path} warn;\n"
            f"zstd_comp_level {level};"
        ),
        extra_locations=(
            "location /workspace/ {\n"
            f"    alias {WORKSPACE_FIXTURE_DIR}/;\n"
            "    default_type application/octet-stream;\n"
            "}\n"
        ),
    )
    start_nginx()
    try:
        # Compress a small static body plus the 1 MB fixture — exercises
        # both single-call and multi-buf paths in the body filter.
        for path in ("/text", f"/workspace/{COMPRESSIBLE_BODY_NAME}"):
            r = requests.get(
                f"{BASE_URL}{path}",
                headers={"Accept-Encoding": "zstd"},
                timeout=10,
                stream=True,
            )
            _ = r.raw.read(decode_content=False)
            r.close()

        time.sleep(0.1)  # let nginx flush deferred WARN entries
        log_content = log_path.read_text() if log_path.exists() else ""
    finally:
        stop_nginx()

    assert "zstd workspace exhausted" not in log_content, (
        f"At zstd_comp_level={level} libzstd needed more than "
        f"ZSTD_estimateCStreamSize budget; fallback path fired. "
        f"Log content:\n{log_content}"
    )
