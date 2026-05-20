"""Direction A -- early-release workspace regression tests.

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

- 1st compression on a fresh worker: delta-RSS ~ 420 KiB (lazy page-in,
  many libzstd workspace pages never faulted in)
- 2nd compression: delta-RSS ~ 5400 KiB (glibc mmap'd a fresh region, full
  workspace pages resident)
- 3rd and subsequent: delta-RSS ~ 0 KiB (region from request 2 is reused;
  pages already resident, no new heap growth)

After Direction A the slow-client window would munmap the workspace chunk,
but the visible RSS effect depends on whether glibc had mmap'd the chunk
in the first place -- a state-dependent decision. A strict assertion is
therefore not robust in either direction on a single-request scenario.

Direction A's correctness gates are owned by the existing harness:
- ASan harness (t/asan.sh) -- freeCStream-vs-pfree ordering UAF
- Valgrind harness (t/valgrind.sh) -- abort-path leak (cleanup handler)
- test_workspace_no_fallback (this module) -- ZSTD_estimateCStreamSize budget

Its memory effect is captured informationally in
test_workspace_memory_observed (this module) -- run with `pytest -s` to
emit the before/after numbers for the commit message.

The win shape Direction A targets is observable in production traffic
profiles where many concurrent requests hold workspace simultaneously
(ab -c 1000 small-response scenario per docs/zstd-memory-optimization
section 'ab -c 1000 keepalive'). Single-request isolated RSS measurement
is dominated by glibc heuristics, not by the fix itself.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
from pathlib import Path

import httpx
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
# Incompressible body — random bytes. Used by the concurrent OOM-guard
# test so each response stays ~1 MB after compression (vs. ~177 B for the
# repetitive compressible fixture). Slow drain × concurrency only
# exercises workspace lifetime if drain takes nontrivial wall-clock.
INCOMPRESSIBLE_BODY_SIZE = 1 * 1024 * 1024  # 1 MB
INCOMPRESSIBLE_BODY_NAME = "incompressible-1mb.bin"


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


def _ensure_incompressible_body() -> Path:
    """Write a deterministic incompressible 1 MB body fixture once per
    session. Uses a fixed seed so the fixture is reproducible across
    runs. Random bytes compress to ~equal-size output at any level,
    which is what makes the slow-drain test exercise workspace lifetime
    rather than blasting through a 177-byte compressed payload."""
    import random
    WORKSPACE_FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    body = WORKSPACE_FIXTURE_DIR / INCOMPRESSIBLE_BODY_NAME
    if not body.exists() or body.stat().st_size != INCOMPRESSIBLE_BODY_SIZE:
        rng = random.Random(0xC57DA)  # deterministic
        body.write_bytes(rng.randbytes(INCOMPRESSIBLE_BODY_SIZE))
    return body


@pytest.fixture(scope="module")
def workspace_nginx():
    """Module-scoped nginx with zstd_comp_level 6 (largest workspace in
    the default test matrix configuration -- ~5.5 MB per ZSTD_estimateCStreamSize)
    and a /workspace/ location aliased to a 1 MB compressible fixture body."""
    _ensure_compressible_body()
    _ensure_incompressible_body()
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

    NOT an anchor -- no strict assertion on deltas. Used to produce
    numerical evidence in the Direction A commit message body. Run with
    `pytest -s` to see the printed table.

    Three checkpoints:
      T0  pre-request baseline
      T1  during slow-client window (300 ms after sending GET, before
          draining body -- compression has completed inside nginx, ctx->done
          has flipped, but request_pool is still alive)
      T2  post fast-drain (separate request, fully read, pool destroyed)

    Pre-fix (current stable HEAD): T1 - T0 holds workspace bytes in
    r->pool->large until pool destroy. T2 - T0 reflects whatever glibc
    keeps from heap-pooled small allocations.

    Post-fix (Direction A): T1 - T0 should be smaller -- the bump allocator's
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

    # Verify T1 actually hit the compression path (otherwise the RSS
    # sample is meaningless -- workspace was never allocated, no fix to
    # measure). Headers-only check; the body is captured in the
    # subsequent recv() iterations we deliberately skip.
    headers_lower = buf.lower()
    assert b"content-encoding: zstd" in headers_lower, (
        f"T1 response did not compress with zstd; raw headers:\n"
        f"{buf!r}"
    )

    # Fast-drain checkpoint (T2)
    r = requests.get(
        f"{BASE_URL}/workspace/{COMPRESSIBLE_BODY_NAME}",
        headers={"Accept-Encoding": "zstd"},
        timeout=10,
        stream=True,
    )
    assert r.headers.get("Content-Encoding") == "zstd", (
        f"T2 response did not compress with zstd; headers:\n{r.headers}"
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
        f"delta-RSS={rss_t1 - rss_t0:>+7}  delta-VSZ={vsz_t1 - vsz_t0:>+7}\n"
        f"  T2 (post fast-drain)  VmRSS={rss_t2:>7}  VmSize={vsz_t2:>7}  "
        f"delta-RSS={rss_t2 - rss_t0:>+7}  delta-VSZ={vsz_t2 - vsz_t0:>+7}\n"
    )

    # Sanity only -- samples succeeded, request hit the compression path.
    assert rss_t0 > 0 and rss_t1 > 0 and rss_t2 > 0, (
        f"VmRSS sampling failed: T0={rss_t0}, T1={rss_t1}, T2={rss_t2}"
    )


def test_concurrent_workspace_pinning_oom_guard(workspace_nginx):
    """Issue #18 class regression: workspace pinning under concurrent
    slow-drain clients must NOT accumulate across the in-flight window.

    Drives N concurrent async httpx clients, each slow-draining the
    incompressible 1 MB fixture at ~100 KiB/s (4 KiB chunks every 40 ms).
    Each request therefore holds an open response for ~10 s while libzstd
    has long finished compressing -- exactly the window Direction A
    targets.

    Memory math at zstd_comp_level 6 (workspace ~5.5 MB per
    ZSTD_estimateCStreamSize(6)):

      pre-Direction-A path: workspace pinned in r->pool->large until
        request_pool teardown. Slow drain delays teardown by ~10 s.
        Peak per-worker memory = N x 5.5 MB held simultaneously.
        N=200 -> ~1.1 GB workspace pinned.

      post-Direction-A path: workspace ngx_pfree'd as soon as ctx->done
        flips inside the body filter (~50 ms after compression starts;
        well before the client drain completes). Peak workspace memory
        collapses to roughly the number of concurrent in-flight
        compressions (CPU-bound, ~ncpu) instead of N.

    Discriminative under --memory: run inside

        docker run --rm --memory=768m --memory-swap=768m \\
            zstd-nginx-test:ubuntu-24.04 \\
            pytest .../test_workspace_rss.py::\\
                test_concurrent_workspace_pinning_oom_guard

    768 MiB is enough for post-fix (workspace freed before drain peak,
    only output chain buffers + nginx baseline remain) but not for
    pre-fix (~1.1 GB pinned workspace + nginx ~> 1.2 GB total -> OOM
    kills the worker, surfacing as a 5xx wave on the open connections).

    Without --memory the test still passes correctness assertions
    (zstd magic, byte length, no upstream exceptions) but won't
    discriminate the pinning regression.

    Test sizing rationale:
      * N=200 -- smallest concurrency that exposes the pre-fix
        workspace accumulation deterministically (mklooss's original
        ab repro was -c 1000 -n 200000; we exercise the same bug class
        in a smaller, deterministic, ~12 s test).
      * 100 KiB/s drain rate -- slow enough that drain >> compression,
        fast enough that body completes within timeout. Drives 1 MB
        body over ~10 s, leaving ~20 s headroom against the 30 s
        per-request timeout.
      * Incompressible body -- guarantees response is ~1 MB after
        compression (random content), keeping drain time deterministic.
        A compressible body would shrink to ~177 B and drain in one
        chunk, defeating the slow-drain shape.
    """
    concurrency = int(os.environ.get("ZSTD_CONCURRENCY", "200"))
    drain_rate_kib_s = 100  # 4 KiB every 40 ms
    chunk_size = 4096
    sleep_per_chunk_s = chunk_size / (drain_rate_kib_s * 1024)
    per_request_timeout_s = 30.0
    url = f"{workspace_nginx}/workspace/{INCOMPRESSIBLE_BODY_NAME}"

    async def slow_drain_request(client: httpx.AsyncClient) -> int:
        # stream + aiter_raw bypasses httpx's auto-zstd decoder. We need
        # the raw zstd frame bytes to validate end-to-end response shape
        # (a regression where the filter fails open and emits plaintext
        # with Content-Encoding: zstd would be silently decoded).
        async with client.stream(
            "GET", url, headers={"Accept-Encoding": "zstd"},
        ) as r:
            assert r.status_code == 200, f"status={r.status_code}"
            assert r.headers.get("content-encoding") == "zstd", (
                f"ce={r.headers.get('content-encoding')!r}"
            )
            body = bytearray()
            async for chunk in r.aiter_raw(chunk_size=chunk_size):
                body.extend(chunk)
                await asyncio.sleep(sleep_per_chunk_s)
        assert bytes(body[:4]) == b"\x28\xb5\x2f\xfd", (
            f"missing zstd magic; first16={bytes(body[:16]).hex()}"
        )
        return len(body)

    async def run_all() -> list:
        limits = httpx.Limits(
            max_connections=concurrency + 10,
            max_keepalive_connections=concurrency + 10,
        )
        async with httpx.AsyncClient(
            timeout=per_request_timeout_s, limits=limits,
        ) as client:
            return await asyncio.gather(
                *(slow_drain_request(client) for _ in range(concurrency)),
                return_exceptions=True,
            )

    start = time.monotonic()
    results = asyncio.run(run_all())
    elapsed_s = time.monotonic() - start

    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, (
        f"{len(failures)}/{concurrency} slow-drain requests failed.\n"
        f"First 3 errors: "
        f"{[type(f).__name__ + ': ' + str(f)[:200] for f in failures[:3]]}\n"
        f"Probable causes: (a) worker OOM-killed because workspace "
        f"pinning regressed and memory limit was insufficient; (b) "
        f"filter SEGV under concurrent customAlloc; (c) connection "
        f"reset because send_timeout fired during slow drain."
    )

    sizes = sorted(results)
    # Incompressible body: post-compression size should be within ~10% of
    # input. Anything significantly smaller means the body wasn't actually
    # randomised (regression in the fixture generator).
    assert sizes[0] > 900_000, (
        f"smallest response only {sizes[0]} bytes -- fixture is not "
        f"incompressible; the test isn't exercising the slow-drain shape"
    )

    # Sanity on drain shape: at 100 KiB/s drain rate, 1 MB takes ~10 s,
    # and 200 concurrent under asyncio drain in roughly that wall-clock
    # (highly parallel). If elapsed << expected, the rate-limit didn't
    # bite -- likely httpx buffered, or the slow-drain sleep wasn't
    # gating the next recv. Floor: 5 s (half of nominal).
    assert elapsed_s >= 5.0, (
        f"drain completed in {elapsed_s:.1f}s for {concurrency} x "
        f"{INCOMPRESSIBLE_BODY_SIZE}B requests at {drain_rate_kib_s} "
        f"KiB/s -- throttle was bypassed."
    )

    print(
        f"\n[concurrent] {concurrency} slow-drain requests in "
        f"{elapsed_s:.1f}s, drain rate {drain_rate_kib_s} KiB/s, "
        f"response sizes {sizes[0]}..{sizes[-1]} bytes"
    )


# ---------------------------------------------------------------------------
# Auto-Window RSS reduction OOM-guard (sibling of the slow-drain pinning
# guard above). Verifies that with a KNOWN Content-Length on the upstream
# response, the auto-window code path (commit 4ddc5c9) shrinks each
# request's CStream workspace from the level-default (~5.5 MB at level=6)
# to the body-sized estimate (~80 KiB for a 4 KiB body, windowLog~=12).
#
# What this test guards against:
#   - regression of the per-request auto-tune in
#     ngx_http_zstd_filter_create_cstream: if a future change reverts the
#     ZSTD_getCParams + ZSTD_estimateCStreamSize_usingCParams path back to
#     ZSTD_estimateCStreamSize(level), each in-flight slow-drain request
#     would once again pin ~5.5 MB of workspace. At 200 concurrent
#     in-flight clients that is ~1.1 GB of peak workspace -- the same
#     pre-Direction-A pinning shape, just triggered by known-C-L workloads
#     instead of chunked ones
#   - regression of the bump-allocator-fit invariant (auto-tuned ws_size
#     must cover libzstd's actual customAlloc demand); a miss would emit
#     "zstd workspace exhausted" at NGX_LOG_ALERT, which we grep below
#
# The auto-window log line itself is emitted at NGX_LOG_INFO (see plan
# Task 2 deviation), not WARN. We keep the error_log at warn level
# because (a) the ALERT-level workspace-exhausted line still surfaces,
# and (b) the test asserts memory/correctness, not log-line presence.
#
# Discriminativeness note: like the sibling OOM-guard above, container
# OOM detection only triggers when this test is launched with a docker
# `--memory=NNNm` cap below ~1.1 GB. Inside the default run.sh (no
# memory cap) the test verifies correctness only. To stress the memory
# ceiling explicitly:
#
#     docker run --rm --memory=192m --memory-swap=192m \\
#         zstd-nginx-test:ubuntu-24.04 \\
#         pytest /opt/regression/test_workspace_rss.py::\\
#             test_workspace_rss_shrinks_under_known_content_length
#
# 192m is the suggested starting point per plan: cgroup overhead +
# nginx baseline ~64m + 200 x ~80 KiB workspace ~= ~80m -> ~144m total
# with margin. Pre-auto-window this would OOM (200 x 5.5 MB = ~1.1 GB
# of pinned workspace far exceeds 192m).


# Distinct from any other test fixture port (test_proxy_flush uses 9004,
# test_auto_window uses 9005).
_AW_RSS_FIXTURE_PORT = 9006
_AW_RSS_BODY_SIZE = 4096  # 4 KiB compressible body per plan


def _aw_rss_body() -> bytes:
    """Compressible 4 KiB pattern. Auto-window picks windowLog from
    ceil(log2(4096)) = 12 -> workspace ~80 KiB. Compressed bytes ~few
    hundred, so the slow drain has to space out the read to actually
    hold the response open for the duration of the test."""
    pattern = (
        b"The quick brown fox jumps over the lazy dog. "
        b"Sphinx of black quartz, judge my vow.\n"
    )
    reps = (_AW_RSS_BODY_SIZE + len(pattern) - 1) // len(pattern)
    return (pattern * reps)[:_AW_RSS_BODY_SIZE]


def _aw_rss_handle(c: socket.socket) -> None:
    """Minimal HTTP/1.1 upstream: serves a known-Content-Length 4 KiB
    body regardless of request path. Connection: close after one
    response (no keepalive needed for this test shape)."""
    try:
        c.settimeout(5)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = c.recv(4096)
            if not chunk:
                break
            buf += chunk
        body = _aw_rss_body()
        c.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
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


def _aw_rss_server_loop(sock: socket.socket,
                        stop_event: threading.Event) -> None:
    sock.settimeout(0.5)
    while not stop_event.is_set():
        try:
            c, _ = sock.accept()
        except socket.timeout:
            continue
        threading.Thread(
            target=_aw_rss_handle, args=(c,), daemon=True
        ).start()


def test_workspace_rss_shrinks_under_known_content_length():
    """Sibling of test_concurrent_workspace_pinning_oom_guard.

    Drives 200 concurrent async httpx clients against a proxy_pass
    location whose upstream sets an explicit Content-Length: 4096. The
    auto-window code path (introduced in commit 4ddc5c9) feeds the
    Content-Length through ZSTD_getCParams + setParameter(windowLog), so
    each in-flight CStream workspace is ~80 KiB rather than the
    level-default ~5.5 MB.

    Assertions:
      (a) every request returns 200 + Content-Encoding: zstd
      (b) decompressed body length matches input
      (c) "zstd workspace exhausted" is ABSENT in the warn-level
          error_log (the bump-allocator-fit invariant still holds with
          the smaller auto-tuned ws_size + headroom)
      (d) no request raises (worker not OOM-killed; if launched under a
          docker --memory cap below ~1.1 GB, this discriminates the
          regression where auto-tune is reverted)
    """
    concurrency = int(os.environ.get("ZSTD_AW_CONCURRENCY", "200"))
    drain_rate_kib_s = 4  # one 4 KiB chunk per second per client
    chunk_size = 1024
    sleep_per_chunk_s = chunk_size / (drain_rate_kib_s * 1024)
    per_request_timeout_s = 30.0
    log_path = Path("/tmp/zstd-aw-rss-test.log")
    log_path.unlink(missing_ok=True)

    # Upstream fixture: function-scoped because this test owns its own
    # nginx lifecycle (custom error_log + proxy_pass location).
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", _AW_RSS_FIXTURE_PORT))
    sock.listen(64)
    stop_event = threading.Event()
    server_thread = threading.Thread(
        target=_aw_rss_server_loop,
        args=(sock, stop_event),
        daemon=True,
    )
    server_thread.start()

    stop_nginx()
    render_template(
        extra_directives=(
            f"error_log {log_path} warn;\n"
            "zstd_comp_level 6;"
        ),
        extra_locations=(
            "location /aw-rss/ {\n"
            f"    proxy_pass http://127.0.0.1:{_AW_RSS_FIXTURE_PORT}/;\n"
            "    proxy_http_version 1.1;\n"
            "    proxy_buffering on;\n"
            "}\n"
        ),
    )
    start_nginx()

    url = f"{BASE_URL}/aw-rss/body"

    async def slow_drain_request(client: httpx.AsyncClient) -> int:
        # Same shape as test_concurrent_workspace_pinning_oom_guard:
        # stream + aiter_raw to bypass httpx's auto-zstd decoder and
        # validate the wire bytes are an actual zstd frame.
        async with client.stream(
            "GET", url, headers={"Accept-Encoding": "zstd"},
        ) as r:
            assert r.status_code == 200, f"status={r.status_code}"
            assert r.headers.get("content-encoding") == "zstd", (
                f"ce={r.headers.get('content-encoding')!r}"
            )
            body = bytearray()
            async for chunk in r.aiter_raw(chunk_size=chunk_size):
                body.extend(chunk)
                await asyncio.sleep(sleep_per_chunk_s)
        assert bytes(body[:4]) == b"\x28\xb5\x2f\xfd", (
            f"missing zstd magic; first16={bytes(body[:16]).hex()}"
        )
        return len(body)

    async def run_all() -> list:
        limits = httpx.Limits(
            max_connections=concurrency + 10,
            max_keepalive_connections=concurrency + 10,
        )
        async with httpx.AsyncClient(
            timeout=per_request_timeout_s, limits=limits,
        ) as client:
            return await asyncio.gather(
                *(slow_drain_request(client) for _ in range(concurrency)),
                return_exceptions=True,
            )

    try:
        start = time.monotonic()
        results = asyncio.run(run_all())
        elapsed_s = time.monotonic() - start
        time.sleep(0.1)  # let nginx flush any deferred WARN entries
    finally:
        stop_nginx()
        stop_event.set()
        server_thread.join(timeout=2)
        sock.close()

    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, (
        f"{len(failures)}/{concurrency} slow-drain requests failed.\n"
        f"First 3 errors: "
        f"{[type(f).__name__ + ': ' + str(f)[:200] for f in failures[:3]]}\n"
        f"Probable causes: (a) worker OOM-killed because auto-window "
        f"regressed and per-request workspace returned to level-default "
        f"size (200 x ~5.5 MB = ~1.1 GB pinned -- only discriminative "
        f"under docker --memory cap); (b) filter SEGV under concurrent "
        f"customAlloc; (c) bump-allocator-fit underestimate -> ALERT "
        f"fallback path (see error_log assertion below)."
    )

    sizes = sorted(results)
    # Compressed 4 KiB of the repeating pattern should be very small
    # (typically <300 B). Floor at 10 B guards against degenerate
    # responses; ceiling at the input size guards against an
    # uncompressed pass-through being silently treated as zstd.
    assert sizes[0] >= 10, (
        f"smallest response only {sizes[0]} bytes -- response shape "
        f"is wrong (truncation? upstream error?)"
    )
    assert sizes[-1] <= _AW_RSS_BODY_SIZE, (
        f"largest response {sizes[-1]} bytes > input {_AW_RSS_BODY_SIZE}"
        f" -- compression failed open or auto-window broke ratio"
    )

    # Vacuous-pass guard: error_log must exist (directive applied).
    assert log_path.exists(), (
        f"expected error_log at {log_path}; directive did not apply or "
        f"nginx never logged. Fix the test harness before treating "
        f"workspace-exhausted as 'never fires'."
    )
    log_content = log_path.read_text()
    assert "zstd workspace exhausted" not in log_content, (
        f"Bump-allocator-fit invariant violated under auto-window: the "
        f"ZSTD_estimateCStreamSize_usingCParams budget did not cover "
        f"libzstd's actual workspace demand for srcSize="
        f"{_AW_RSS_BODY_SIZE}. Fallback path fired. Log content:\n"
        f"{log_content}"
    )

    print(
        f"\n[aw-rss] {concurrency} slow-drain known-C-L requests in "
        f"{elapsed_s:.1f}s, drain rate {drain_rate_kib_s} KiB/s, "
        f"compressed sizes {sizes[0]}..{sizes[-1]} bytes "
        f"(input {_AW_RSS_BODY_SIZE} B)"
    )


@pytest.mark.parametrize("level", [1, 3, 6, 9])
def test_workspace_no_fallback(level):
    """Sanity: ZSTD_estimateCStreamSize(level) must cover libzstd's actual
    internal allocation needs.

    After Direction A this test catches regressions where a future
    libzstd version's workspace requirement exceeds the estimate --
    which would force the fallback `ngx_palloc(r->pool, size)` path
    and emit the warning.

    Uses an http-context error_log directive to capture WARN+ events to a
    file we can grep, since the default template logs to /dev/stderr.

    This test manages its own nginx lifecycle (stops + restarts per
    parametrize) and is declared LAST in the module so any pytest
    scheduling that runs it after workspace_nginx-using tests doesn't
    leave the module-scoped fixture's nginx in a stopped state -- module
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
        # Compress a small static body plus the 1 MB fixture -- exercises
        # both single-call and multi-buf paths in the body filter.
        for path in ("/text", f"/workspace/{COMPRESSIBLE_BODY_NAME}"):
            r = requests.get(
                f"{BASE_URL}{path}",
                headers={"Accept-Encoding": "zstd"},
                timeout=10,
                stream=True,
            )
            assert r.headers.get("Content-Encoding") == "zstd", (
                f"path {path} did not compress; headers:\n{r.headers}"
            )
            _ = r.raw.read(decode_content=False)
            r.close()

        time.sleep(0.1)  # let nginx flush deferred WARN entries
    finally:
        stop_nginx()

    # Guard against vacuous pass: if the error_log file is absent the
    # `not in ""` check would silently succeed even when the directive
    # failed to apply. nginx must have produced this file via the
    # `error_log` directive above.
    assert log_path.exists(), (
        f"expected error_log at {log_path}; directive did not apply or "
        f"nginx never logged. The vacuous-pass guard caught this -- fix "
        f"the test harness before treating fallback as 'never fires'."
    )
    log_content = log_path.read_text()

    assert "zstd workspace exhausted" not in log_content, (
        f"At zstd_comp_level={level} libzstd needed more than "
        f"ZSTD_estimateCStreamSize budget; fallback path fired. "
        f"Log content:\n{log_content}"
    )
