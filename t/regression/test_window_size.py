"""Browser-compatibility regression for the zstd response frame header's
Window Size field.

Chrome and Firefox both impose an 8 MiB upper bound on the zstd window size
they accept from servers. A response whose first frame declares a window
beyond that fails with:
  * net::ERR_ZSTD_WINDOW_SIZE_TOO_BIG (Chrome)
  * "Unsupported compression method" (Firefox)

Upstream tracker: tokers/zstd-nginx-module#35 (CLOSED, but the underlying
operator foot-gun remains: configure zstd_window_bits 24+ and your
responses become unparseable by browsers while curl-style clients continue
working — silent-misconfiguration disaster).

This test verifies the module's DEFAULT configuration (no zstd_window_bits
override) produces a browser-safe window. On master baseline the libzstd
default for level 1 is windowLog=19 → 512 KiB, well below the 8 MiB
ceiling, so this is a regression net: any future change to the in-module
default that flips it browser-hostile would fail here.

Not covered (V2): asserting that an explicit zstd_window_bits 24 produces
> 8 MiB. The directive is step1-only; on master baseline it doesn't exist.
"""

from __future__ import annotations

import re
import subprocess

import requests

BROWSER_LIMIT_BYTES = 8 * 1024 * 1024  # 8 MiB = 2^23


def _window_bytes(zst_path: str) -> int | None:
    """Run `zstd --list -v` on a .zst file, return its frame Window Size in
    bytes. Returns None if parsing fails (unexpected format)."""
    r = subprocess.run(
        ["zstd", "--list", "-v", zst_path],
        capture_output=True, text=True, check=False,
    )
    # Line shape: "Window Size: 512 KiB (524288 B)" — parenthesised value
    # is authoritative, decimal integer in bytes.
    m = re.search(r"Window Size:.*?\((\d+)\s*B\)", r.stdout)
    return int(m.group(1)) if m else None


def test_window_size_default_browser_safe(nginx, tmp_path):
    """The default-configured filter must produce a response whose frame
    header reports Window Size <= 8 MiB (Chrome's hard limit). Today's
    module default is windowLog=19 (512 KiB), so this is a regression net
    against any future change to a browser-hostile default."""
    # /text is a 180-byte text/plain body served by the baseline template;
    # plenty of body for the encoder to commit to a real window size.
    # (Tiny bodies < frame block size could produce a degenerate window.)
    r = requests.get(
        nginx + "/text",
        headers={"Accept-Encoding": "zstd"},
        timeout=5,
    )
    assert r.headers.get("Content-Encoding") == "zstd", (
        f"expected Content-Encoding=zstd, got {r.headers.get('Content-Encoding')!r}"
    )
    zst_path = tmp_path / "response.zst"
    zst_path.write_bytes(r.content)

    bytes_ = _window_bytes(str(zst_path))
    assert bytes_ is not None, (
        f"could not parse `zstd --list -v` output; stdout was empty or "
        f"unexpected format. Response was {len(r.content)} compressed bytes."
    )
    assert bytes_ <= BROWSER_LIMIT_BYTES, (
        f"window={bytes_}B exceeds Chrome 8 MiB limit ({BROWSER_LIMIT_BYTES}B) — "
        f"browsers will reject this response with ERR_ZSTD_WINDOW_SIZE_TOO_BIG"
    )
