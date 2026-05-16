"""Pytest port of t/regression/dict-reload.sh.

Task 7: CDict allocated by ngx_http_zstd_filter_merge_loc_conf when
zstd_dict_file is set must be freed on cycle teardown (pool cleanup hook).
Before the fix every `nginx -s reload` leaked one CDict's worth of memory.

Coverage:
  * Render config with zstd_dict_file pointing at a small dict.
  * Warmup batch: 50 reloads — lets glibc allocator caches plateau.
  * Measurement batch: 50 more reloads — RSS delta must stay near zero
    (≤ 512 KiB ceiling absorbs nginx-core / glibc noise under emulation).
  * Sanity: nginx still serves baseline after the reload storm.

Skipped under ZSTD_REGRESSION_NO_DAEMON=1 — `nginx -s reload` needs the
master/worker model, which single-process foreground doesn't provide.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
import requests

from conftest import (
    BASE_URL,
    _nginx_has_compat,
    render_template,
    start_nginx,
    stop_nginx,
)

DICT_FILE = Path("/var/fixtures/zstd-dict-reload")

# Skip under single-process foreground — no master, reload semantics don't
# apply. asan.sh / valgrind.sh use NO_DAEMON; run.sh / coverage.sh don't.
pytestmark = pytest.mark.skipif(
    bool(os.environ.get("ZSTD_REGRESSION_NO_DAEMON")),
    reason="dict-reload needs master/worker model (single-process mode set)",
)

PID_PATH = Path("/tmp/nginx.pid")


@pytest.fixture(scope="module")
def reload_nginx():
    Path("/var/fixtures").mkdir(parents=True, exist_ok=True)
    DICT_FILE.write_bytes(b"D" * 4096)

    load_modules = (
        "load_module modules/ngx_http_zstd_filter_module.so;"
        if _nginx_has_compat() else ""
    )
    stop_nginx()
    render_template(
        extra_directives=f"zstd_dict_file {DICT_FILE};",
        load_modules=load_modules,
    )
    start_nginx()
    try:
        yield BASE_URL
    finally:
        stop_nginx()


def _master_rss_kib(pid: int) -> int:
    """VmRSS in KiB from /proc/<pid>/status. Returns 0 on read failure."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except FileNotFoundError:
        pass
    return 0


def _do_reloads(n: int, conf: str = "/etc/nginx/nginx.conf") -> None:
    for i in range(1, n + 1):
        r = subprocess.run(
            ["nginx", "-c", conf, "-s", "reload"],
            capture_output=True, text=True, check=False,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"nginx -s reload #{i} failed:\n{r.stdout}\n{r.stderr}"
            )
        # Master forks new workers + retires old ones; give it a beat.
        time.sleep(0.05)
    # Let old-cycle teardown settle.
    time.sleep(1.0)


def test_no_rss_growth_across_reloads(reload_nginx):
    """Warmup 50 reloads → sample baseline → 50 more → assert delta ≤ 512 KiB.

    Buggy code compounds at ~tens of KiB per cycle (CDict + libzstd-internal
    tables); fixed code stays near zero. 512 KiB ceiling absorbs glibc
    allocator and nginx-core noise under qemu/Rosetta emulation.
    """
    master_pid = int(PID_PATH.read_text().strip())

    _do_reloads(50)
    baseline = _master_rss_kib(master_pid)

    _do_reloads(50)
    after = _master_rss_kib(master_pid)
    delta = after - baseline

    assert delta <= 512, (
        f"RSS grew {delta} KiB over 50 post-warmup reloads "
        f"(baseline={baseline} KiB after={after} KiB; must be ≤ 512 KiB)"
    )


def test_still_serving_after_reload_storm(reload_nginx):
    """Sanity: the cleanup path runs at pool-destroy. A buggy implementation
    that double-frees would have crashed the master long before this point —
    so a successful baseline GET confirms the master is healthy."""
    r = requests.get(reload_nginx + "/", timeout=2)
    assert r.status_code == 200
    assert r.text.strip() == "ok"
