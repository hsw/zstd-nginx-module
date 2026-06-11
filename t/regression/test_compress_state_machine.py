"""Observable-behaviour regression tests for the zstd body filter's
compress entry shapes.

Post-Task-3 (V2 per-call-op refactor) the filter no longer carries an
explicit action state machine; flush/end op derivation is per-call from
the sticky-finish flags. These tests assert decompressed byte-equality
across the entry shapes that previously exercised distinct state-machine
branches — coverage is now black-box (request → response → decompress
→ compare to ground truth) rather than whitebox.

Each parametrized case exercises a distinct entry shape:
  * single-byte      — minimal terminate-immediately path
  * h1-131072        — HTTP/1.1 counterpart to h2-truncation (chain-link
                       boundary at ZSTD_CStreamInSize())
  * h1-200000        — multi-iteration drain at scale
  * disk-vs-proxy    — disk static file vs proxy_pass (in_file buffer vs
                       in-memory chain link) — decompressed must match
                       byte-for-byte

Plus two non-parametrized tests:
  * keep_alive_three — three requests on one TCP connection (num_connects=1)
  * parallel_burst   — 20 concurrent connections, mixed sizes, re-entrancy

Coverage scope: byte-equality + valid framing across all entry shapes.
The production flush-promotion latency bug is exercised by
`test_proxy_flush.py` (chunked-off-tiny sub-test). Empty-body contracts
(known CL=0 declined per C12-2; chunked-empty sentinel-buf path) live in
`test_empty_body.py`.
"""

from __future__ import annotations

import concurrent.futures
import http.client
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pytest

from conftest import (
    BASE_URL,
    http_request,
    render_template,
    start_nginx,
    stop_nginx,
)


EXTRA_LOCATIONS = """
    # Single-byte body — minimal viable input.
    location = /single {
        add_header Content-Type text/plain;
        return 200 "a";
    }
    # Equivalence path: proxy back to ourselves serving the static file,
    # strip Accept-Encoding so the inner location returns RAW bytes (no
    # double-compress).
    location = /equiv-proxy/ {
        proxy_pass http://127.0.0.1:8080/random/equiv.css;
        proxy_http_version 1.1;
        proxy_set_header Accept-Encoding "";
    }
"""


def _prepare_fixtures() -> Path:
    fixtures = Path("/var/fixtures/random")
    fixtures.mkdir(parents=True, exist_ok=True)
    for n in (131072, 200000):
        f = fixtures / str(n)
        if not f.exists():
            with f.open("wb") as fh:
                fh.write(__import__("os").urandom(n))
    # Repetitive CSS-like body for disk-vs-proxy equivalence.
    css = fixtures / "equiv.css"
    if not css.exists():
        with css.open("w") as fh:
            for i in range(400):
                fh.write(f".btn-{i} {{ color: #abcdef; padding: 4px; }}\n")
    return fixtures


@pytest.fixture(scope="module")
def nginx_state_machine() -> Iterator[str]:
    """Module-scoped nginx with empty/single/equiv-proxy locations + the
    /random/ alias from the baseline template."""
    _prepare_fixtures()
    stop_nginx()
    render_template(extra_locations=EXTRA_LOCATIONS.strip())
    start_nginx()
    try:
        yield BASE_URL
    finally:
        stop_nginx()


@dataclass
class BasicCase:
    label: str
    path: str
    # None → expect HTTP body to decompress to empty bytes; bytes → exact
    # match; Path → byte-compare against file.
    expect: bytes | Path | None


def _decompress(body: bytes, tmp_path: Path, label: str) -> bytes:
    zst = tmp_path / f"{label}.zst"
    zst.write_bytes(body)
    r = subprocess.run(
        ["zstd", "-dc", str(zst)], capture_output=True, check=False
    )
    assert r.returncode == 0, (
        f"[{label}] zstd -d failed: {r.stderr.decode(errors='replace')}"
    )
    return r.stdout


BASIC_CASES = [
    BasicCase("single", "/single", b"a"),
    BasicCase("h1-131072", "/random/131072",
              Path("/var/fixtures/random/131072")),
    BasicCase("h1-200000", "/random/200000",
              Path("/var/fixtures/random/200000")),
]


@pytest.mark.parametrize("case", BASIC_CASES, ids=[c.label for c in BASIC_CASES])
def test_state_machine_basic(nginx_state_machine, case: BasicCase, tmp_path):
    """Hit one of the configured locations under HTTP/1.1, decode response
    body, byte-compare against expected payload. Uses conftest.http_request
    so we read the raw zstd frame off the wire (not urllib3-decoded)."""
    r, body = http_request(
        nginx_state_machine, case.path, accept_encoding="zstd", timeout=15,
    )
    assert r.status_code == 200, (
        f"[{case.label}] status={r.status_code}, headers={dict(r.headers)}"
    )

    assert r.headers.get("Content-Encoding") == "zstd", (
        f"[{case.label}] Content-Encoding={r.headers.get('Content-Encoding')!r}, "
        f"expected zstd"
    )
    assert body[:4] == b"\x28\xb5\x2f\xfd", (
        f"[{case.label}] body missing zstd magic; hex={body[:16].hex()}"
    )
    decoded = _decompress(body, tmp_path, case.label)
    if isinstance(case.expect, Path):
        expect_bytes = case.expect.read_bytes()
    else:
        expect_bytes = case.expect or b""
    assert decoded == expect_bytes, (
        f"[{case.label}] decoded differs: expected {len(expect_bytes)}B "
        f"got {len(decoded)}B"
    )


def test_state_machine_disk_vs_proxy(nginx_state_machine, tmp_path):
    """Same payload served two ways must decompress to identical bytes:
    Path 1: /random/equiv.css via static `alias` (in_file buffer)
    Path 2: /equiv-proxy/ via proxy_pass + Accept-Encoding strip (in-memory
            chain link from upstream connection)
    The compression filter sees structurally different chain shapes; the
    decompressed output must be byte-identical regardless. Documents the
    equivalence the V2 compressStream2 migration must preserve."""
    src = Path("/var/fixtures/random/equiv.css").read_bytes()

    def fetch_decompress(path: str) -> bytes:
        r, body = http_request(
            nginx_state_machine, path, accept_encoding="zstd", timeout=15,
        )
        assert r.headers.get("Content-Encoding") == "zstd", (
            f"path={path} Content-Encoding={r.headers.get('Content-Encoding')!r}"
        )
        return _decompress(body, tmp_path, path.replace("/", "_"))

    disk = fetch_decompress("/random/equiv.css")
    proxy = fetch_decompress("/equiv-proxy/")
    assert disk == src, "disk decoded != origin source"
    assert proxy == src, "proxy decoded != origin source"
    assert disk == proxy, "disk decoded != proxy decoded (path divergence)"


def test_keep_alive_three(nginx_state_machine, tmp_path):
    """Three sequential compressed responses on ONE TCP connection. Each
    request gets a fresh ctx + ZSTD_CStream — there should be no state
    leak between requests on the same worker connection.

    Uses http.client directly so we can assert "one connection" by simply
    not closing between requests."""
    conn = http.client.HTTPConnection("127.0.0.1", 8080, timeout=15)
    requests_made = [
        ("/random/131072", Path("/var/fixtures/random/131072")),
        ("/random/200000", Path("/var/fixtures/random/200000")),
        ("/text", None),  # baseline 180-byte body, just verify decode
    ]
    try:
        for i, (path, expect_file) in enumerate(requests_made, 1):
            conn.request("GET", path, headers={"Accept-Encoding": "zstd"})
            resp = conn.getresponse()
            assert resp.status == 200, (
                f"req{i} {path}: status={resp.status}"
            )
            assert resp.getheader("Content-Encoding") == "zstd", (
                f"req{i} {path}: Content-Encoding="
                f"{resp.getheader('Content-Encoding')!r}"
            )
            body = resp.read()
            decoded = _decompress(body, tmp_path, f"ka{i}")
            assert len(decoded) > 0, f"req{i} {path}: empty decoded body"
            if expect_file is not None:
                assert decoded == expect_file.read_bytes(), (
                    f"req{i} {path}: decoded differs from source"
                )
    finally:
        conn.close()


def test_parallel_burst(nginx_state_machine, tmp_path):
    """20 concurrent connections, mixed body sizes. Exercises re-entrancy
    of the state machine init/teardown across requests in the same worker.
    Uses ThreadPoolExecutor with 20 workers — each gets its own connection,
    so the worker process handles 20 simultaneous compression contexts.

    Uses conftest.http_request (raw bytes via stream + decode_content=False)
    so each thread compares against the on-the-wire zstd frame, not
    urllib3-decoded content."""
    urls = [
        "/random/131072",
        "/random/200000",
        "/text",
        "/single",
    ]

    def fetch(i: int) -> tuple[int, str | None]:
        path = urls[i % 4]
        try:
            r, body = http_request(
                nginx_state_machine, path, accept_encoding="zstd", timeout=30,
            )
        except Exception as e:
            return i, f"request-exception: {e}"
        if r.status_code != 200:
            return i, f"status={r.status_code}"
        if r.headers.get("Content-Encoding") != "zstd":
            return i, f"ce={r.headers.get('Content-Encoding')!r}"
        if body[:4] != b"\x28\xb5\x2f\xfd":
            return i, f"missing zstd magic; hex={body[:16].hex()}"
        # Decompress on a per-thread temp file to avoid contention.
        zst = tmp_path / f"burst-{i}.zst"
        zst.write_bytes(body)
        rc = subprocess.run(
            ["zstd", "-dc", str(zst)], capture_output=True, check=False
        )
        if rc.returncode != 0:
            return i, f"decode-fail: {rc.stderr.decode(errors='replace')[:80]}"
        return i, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(fetch, range(20)))
    failures = [(i, msg) for i, msg in results if msg is not None]
    assert not failures, (
        f"parallel burst had {len(failures)} failures: {failures[:5]}"
    )
