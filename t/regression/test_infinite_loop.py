"""Pytest port of t/regression/infinite-loop.sh.

PR #23 / Task 4: when upstream advertises Content-Length: N but sends N-1
bytes then closes, the original filter spun forever (worker CPU 100%). Fix:
break out on EOF before declared length.

Coverage:
  1. python TCP fixture on :9004 sends Content-Length: 16385 but only
     16384 bytes of body, then closes (1-byte short read).
  2. nginx proxies /shortread/ → :9004 with proxy_buffering off (so short-read
     flows straight into the body filter).
  3. curl with Accept-Encoding: zstd + --max-time 5 must return within 8s.
  4. Worker CPU jiffies over 2s after the request must stay near zero
     (delta_ms ≤ 200) — a spinning worker would burn ~2000 ms.
"""

from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from conftest import (
    BASE_URL,
    _nginx_has_compat,
    nginx_worker_pid_from_master,
    render_template,
    start_nginx,
    stop_nginx,
)

PID_PATH = Path("/tmp/nginx.pid")
FIXTURE_PORT = 9004  # distinct from test_filter_eligibility (9000), test_websocket (9001), test_http2_proxy_flush (9002), test_proxy_flush (9003)


def _start_short_read_fixture() -> threading.Thread:
    """TCP server that lies about Content-Length: claims 16385, sends 16384,
    closes — exact pattern that triggered the original infinite loop."""
    body = b"x" * 16384

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
                b"Content-Length: " + str(len(body) + 1).encode() + b"\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                + body
            )
        finally:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()

    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", FIXTURE_PORT))
    s.listen(8)

    def loop():
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(c,), daemon=True).start()

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    t._sock = s  # type: ignore[attr-defined]
    return t


@pytest.fixture(scope="module")
def loop_nginx():
    load_modules = (
        "load_module modules/ngx_http_zstd_filter_module.so;"
        if _nginx_has_compat() else ""
    )
    fixture_thread = _start_short_read_fixture()

    # We need proxy_buffering off so the short-read flows straight through
    # the zstd body filter (default proxy_buffering on may absorb the entire
    # stream before the filter ever sees it, masking the bug). The template
    # already defines /shortread/ with default buffering — can't override (would
    # be "duplicate location"). Use a separate path /shortread/ with the
    # required buffering-off setting.
    extra_locations = f"""
        location /shortread/ {{
            proxy_pass http://127.0.0.1:{FIXTURE_PORT}/;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_buffering off;
        }}
"""
    stop_nginx()
    render_template(extra_locations=extra_locations, load_modules=load_modules)
    start_nginx()
    try:
        yield BASE_URL
    finally:
        stop_nginx()
        try:
            fixture_thread._sock.close()  # type: ignore[attr-defined]
        except Exception:
            pass


def _worker_cpu_jiffies(worker_pid: int) -> int:
    """utime + stime from /proc/<pid>/stat — sum jiffies. Raises on read
    failure: a missing /proc/<pid>/stat means the worker has died (or
    we sampled the wrong pid). Silently returning 0 would let the spin
    test return delta=0 and conclude "not spinning" — i.e. mask a
    crash. Hard-fail instead."""
    try:
        fields = Path(f"/proc/{worker_pid}/stat").read_text().split()
    except FileNotFoundError as e:
        raise AssertionError(
            f"/proc/{worker_pid}/stat missing — worker crashed or pid is wrong"
        ) from e
    return int(fields[13]) + int(fields[14])


def test_no_hang_under_short_read(loop_nginx):
    """curl with --max-time 5 must return well under 8s."""
    start = time.monotonic()
    r = subprocess.run(
        [
            "curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
            "-H", "Accept-Encoding: zstd",
            "--max-time", "5",
            f"{loop_nginx}/shortread/",
        ],
        capture_output=True, text=True, check=False,
    )
    elapsed = time.monotonic() - start
    assert elapsed < 8, f"curl took {elapsed:.1f}s (must be < 8s); rc={r.returncode}"


def test_worker_not_spinning(loop_nginx):
    """After the short-read request returns, the worker must drop to idle.
    Sample CPU jiffies over 2s; spinning worker would burn ~2000ms, idle
    worker stays near zero. Ceiling 200ms absorbs scheduling jitter under
    qemu / Rosetta emulation.

    Under ZSTD_REGRESSION_NO_DAEMON=1 (asan.sh / valgrind.sh) nginx runs
    with `master_process off`, so there is no child worker — the master
    pid IS the worker. nginx_worker_pid_from_master() returns None in
    that case; per its docstring the caller treats None as "use the
    master pid as the worker"."""
    master_pid_str = PID_PATH.read_text().strip()
    master_pid = int(master_pid_str)
    worker_pid = nginx_worker_pid_from_master(master_pid)
    if worker_pid is None:
        # master_process off — master IS the worker. Only valid under
        # ZSTD_REGRESSION_NO_DAEMON=1 (asan.sh / valgrind.sh). In daemon
        # mode the lookup must succeed; falling back to master_pid would
        # sample the idle master and miss a spinning worker.
        assert os.environ.get("ZSTD_REGRESSION_NO_DAEMON"), (
            f"nginx_worker_pid_from_master({master_pid}) returned None in "
            f"daemon mode — refusing to sample master (would mask spinning "
            f"worker)"
        )
        worker_pid = master_pid

    # Drive a request first so the worker is awake.
    subprocess.run(
        ["curl", "-sS", "-o", "/dev/null",
         "-H", "Accept-Encoding: zstd",
         "--max-time", "5", f"{loop_nginx}/shortread/"],
        capture_output=True, check=False,
    )

    clk_tck = os.sysconf("SC_CLK_TCK")
    before = _worker_cpu_jiffies(worker_pid)
    time.sleep(2)
    after = _worker_cpu_jiffies(worker_pid)
    delta_ms = (after - before) * 1000 // clk_tck

    assert delta_ms <= 200, (
        f"worker CPU delta {delta_ms} ms over 2s "
        f"(must be ≤ 200 ms; spinning worker would burn ~2000 ms)"
    )
