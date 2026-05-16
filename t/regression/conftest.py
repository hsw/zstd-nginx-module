"""pytest fixtures shared by t/regression/test_*.py modules.

POC: covers only what test_accept_encoding.py needs. Mirrors the bash helpers
in _common.sh — same template-render and graceful-stop semantics so a pytest
run inside the existing docker images produces identical behaviour to the
.sh scripts (no separate harness mode required).

If this POC turns into a full migration:
  * Add per-worker port fixtures for pytest-xdist parallelism.
  * Wrap nginx -V detection in a session-scoped fixture so we don't shell
    out per-test.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest
import requests

TEMPLATE_PATH = Path("/etc/nginx/templates/nginx.conf.template")
CONF_PATH = Path("/etc/nginx/nginx.conf")
PID_PATH = Path("/tmp/nginx.pid")
PORT = 8080
BASE_URL = f"http://127.0.0.1:{PORT}"


def _nginx_has_compat() -> bool:
    # Same logic the .sh scripts use to decide whether to emit `load_module`.
    out = subprocess.run(
        ["nginx", "-V"], capture_output=True, text=True, check=False
    )
    return "--with-compat" in (out.stdout + out.stderr)


def render_template(
    extra_directives: str = "",
    extra_locations: str = "",
    extra_server: str = "",
    load_modules: str | None = None,
) -> None:
    """Render the nginx.conf.template into /etc/nginx/nginx.conf.

    Only standalone marker lines are substituted — marker mentions in the
    template's comment block stay intact. This mirrors render_nginx_template
    in t/regression/_common.sh.

    extra_server is server-scope content (e.g. extra `listen 8443 ssl;` +
    ssl_certificate directives) injected just inside the server block,
    before the location definitions. Used by HTTP/2 tests that need a TLS
    listener so httpx can negotiate h2 via ALPN.
    """
    if load_modules is None:
        load_modules = (
            "load_module modules/ngx_http_zstd_filter_module.so;"
            if _nginx_has_compat()
            else ""
        )

    with TEMPLATE_PATH.open() as f:
        lines = f.readlines()

    out_lines: list[str] = []
    for line in lines:
        s = line.strip()
        if s == "__LOAD_MODULES__":
            if load_modules:
                out_lines.append(load_modules + "\n")
        elif s == "__EXTRA_DIRECTIVES__":
            if extra_directives:
                out_lines.append(extra_directives + "\n")
        elif s == "__EXTRA_SERVER__":
            if extra_server:
                out_lines.append(extra_server + "\n")
        elif s == "__EXTRA_LOCATIONS__":
            if extra_locations:
                out_lines.append(extra_locations + "\n")
        else:
            out_lines.append(line.replace("__SERVER_PORT__", str(PORT)))

    CONF_PATH.write_text("".join(out_lines))
    _apply_daemon_mode()


def _apply_daemon_mode() -> None:
    """Match the daemon/master_process directives to ZSTD_REGRESSION_NO_DAEMON,
    same as the .sh helper apply_daemon_mode. Pytest doesn't need the
    single-process foreground mode itself, but the env var is set by
    t/asan.sh and t/valgrind.sh and we want their semantics to carry through
    when pytest is invoked from those drivers."""
    txt = CONF_PATH.read_text()
    if os.environ.get("ZSTD_REGRESSION_NO_DAEMON"):
        if "master_process " not in txt:
            txt = txt.replace(
                "daemon off;", "daemon off;\nmaster_process off;"
            )
    else:
        txt = txt.replace("daemon off;", "daemon on;")
    CONF_PATH.write_text(txt)


def _wait_for_listen(timeout_s: float = 6.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            requests.get(BASE_URL + "/", timeout=1).raise_for_status()
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"nginx did not start within {timeout_s}s")


def stop_nginx() -> None:
    """Graceful stop with SIGTERM/SIGKILL escalation — mirrors stop_local_nginx
    in _common.sh. Idempotent."""
    subprocess.run(
        ["nginx", "-c", str(CONF_PATH), "-s", "stop"],
        capture_output=True,
        check=False,
    )
    for _ in range(20):
        if not PID_PATH.exists():
            break
        time.sleep(0.1)
    # Force-kill anything still around.
    if PID_PATH.exists() or _pgrep_nginx():
        subprocess.run(["pkill", "-TERM", "-x", "nginx"], check=False)
        for _ in range(30):
            if not _pgrep_nginx():
                break
            time.sleep(0.1)
        subprocess.run(["pkill", "-KILL", "-x", "nginx"], check=False)
    PID_PATH.unlink(missing_ok=True)


def _pgrep_nginx() -> bool:
    r = subprocess.run(
        ["pgrep", "-x", "nginx"], capture_output=True, check=False
    )
    return r.returncode == 0


def start_nginx() -> None:
    """Test-render the config, then start it — mirrors start_local_nginx_bg.
    Under harness mode (ZSTD_REGRESSION_NO_DAEMON=1) nginx stays in the
    foreground; we background it so the test can keep running."""
    r = subprocess.run(
        ["nginx", "-c", str(CONF_PATH), "-t"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"nginx -t failed:\n{r.stdout}\n{r.stderr}")

    if os.environ.get("ZSTD_REGRESSION_NO_DAEMON"):
        subprocess.Popen(
            ["nginx", "-c", str(CONF_PATH)],
            stdout=subprocess.DEVNULL, stderr=None,
            start_new_session=True,
        )
    else:
        subprocess.run(["nginx", "-c", str(CONF_PATH)], check=True)

    _wait_for_listen()


def zstd_decompress(data: bytes) -> bytes:
    """Decompress a streaming zstd frame produced by nginx (no content-size
    in frame header — streaming encoder doesn't know the total size).

    ZstdDecompressor().decompress() requires the frame's content-size to be
    set in the header; nginx's streaming output doesn't set it. Use
    decompressobj() instead — it streams output without requiring pre-known
    size.
    """
    import zstandard
    return zstandard.ZstdDecompressor().decompressobj().decompress(data)


def nginx_worker_pid_from_master(master_pid: int) -> int | None:
    """Find the first child of nginx master via /proc/<pid>/task/<pid>/children.
    Linux-canonical, works under Rosetta on macOS docker. Returns None if
    no child found OR (single-process foreground mode) the master IS the
    worker — caller should treat None as "use master_pid as worker".
    """
    try:
        line = Path(f"/proc/{master_pid}/task/{master_pid}/children").read_text().strip()
    except FileNotFoundError:
        return None
    if not line:
        return None
    return int(line.split()[0])


def http_request(
    url: str,
    path: str = "/",
    method: str = "GET",
    accept_encoding: str | None = None,
    timeout: float = 5.0,
    data: bytes | str | None = None,
) -> tuple[requests.Response, bytes]:
    """HTTP request helper that defeats two pieces of urllib3 magic that
    interfere with content-negotiation tests:

      1. urllib3 auto-adds `Accept-Encoding: gzip, deflate, zstd` whenever
         python-zstandard is importable. We need full control over the AE
         header for parser tests, so we ALWAYS set it explicitly (passing
         None defaults to `identity` — tells the server "no encoding").
      2. urllib3 auto-decompresses zstd response bodies, mangling raw-byte
         comparisons. We use `stream=True` + `raw.read(decode_content=False)`
         to get the bytes nginx actually put on the wire.

    Returns (response, raw_bytes). Response's headers/status are reliable;
    response.content is NOT — use the second tuple element instead.
    """
    headers = {"Accept-Encoding": accept_encoding if accept_encoding is not None else "identity"}
    r = requests.request(
        method, url + path, headers=headers, timeout=timeout,
        stream=True, data=data,
    )
    body = r.raw.read(decode_content=False) if method != "HEAD" else b""
    r.close()
    return r, body


def nginx_t(extra_directives: str = "", extra_locations: str = "") -> tuple[int, str]:
    """Render the template with the given extras and run `nginx -t` against it.

    Returns (returncode, combined_output). The caller decides whether the
    test expects success or failure — both are useful for config-validation.
    Does NOT start nginx; use the nginx fixture for that.
    """
    render_template(extra_directives=extra_directives, extra_locations=extra_locations)
    r = subprocess.run(
        ["nginx", "-c", str(CONF_PATH), "-t"],
        capture_output=True, text=True, check=False,
    )
    return r.returncode, (r.stdout + r.stderr)


@pytest.fixture(scope="session")
def valid_dict(tmp_path_factory):
    """Session-scoped: small valid zstd dictionary file referenced by tests
    that need an existing readable file path for zstd_dict_file."""
    fixture_dir = Path("/var/fixtures/config-validation")
    fixture_dir.mkdir(parents=True, exist_ok=True)
    path = fixture_dir / "valid.dict"
    path.write_bytes(b"D" * 4096)
    return str(path)


@pytest.fixture(scope="session")
def libzstd_supports_negative_levels() -> bool:
    """libzstd 1.5+ exposes ZSTD_minCLevel() < 0; older versions reject
    negative comp_level at configure time. Used to skip the negative-level
    test on older bases."""
    r = subprocess.run(
        ["zstd", "--version"], capture_output=True, text=True, check=False
    )
    v = r.stdout
    import re
    m = re.search(r"v(\d+)\.(\d+)", v)
    if not m:
        return False
    major, minor = int(m.group(1)), int(m.group(2))
    return (major, minor) >= (1, 5)


@pytest.fixture(scope="module")
def nginx():
    """Module-scoped: each test_*.py gets one nginx instance, reused across
    all its parametrized cases. POC uses the default template (no extra
    directives/locations) — tests that need a different config will need
    their own fixture (or a parametrized variant of this one)."""
    stop_nginx()  # defensive: clean up anything left over.
    render_template()
    start_nginx()
    try:
        yield BASE_URL
    finally:
        stop_nginx()
