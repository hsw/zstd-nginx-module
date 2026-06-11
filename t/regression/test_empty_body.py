"""Empty-body contracts: known Content-Length: 0 vs chunked-empty.

C12-2: a response with a *known* `Content-Length: 0` must NOT be compressed,
regardless of `zstd_min_length` (the fix declines `content_length_n == 0` in
the header filter — nothing is ever gained by compressing an empty body;
before the fix it got `Content-Encoding: zstd`, a pointless ~13-byte empty
zstd frame, and a full baseline workspace on the known-CL auto-window path).
The baseline template already sets `zstd_min_length 0;` at http level, which
is the only configuration under which the default min_length=20 gate does not
already cover this; the location below restates it so the test stays
self-contained if the template default ever changes.

Chunked-empty (unknown length) is the deliberate opposite contract: at header
time `content_length_n == -1`, so the filter cannot know the body is empty —
`Content-Encoding: zstd` is committed before any body arrives, and the body
filter must still emit a valid *empty* zstd frame via its signal-only
sentinel-buf path (`ZSTD_e_end` on zero input bytes). Emitting 0 bytes would
be a malformed `Content-Encoding: zstd` body. This matches nginx gzip
(empty chunked -> empty gzip stream) and is out of C12-2 scope by design.
"""

import socket
import threading
from pathlib import Path
from typing import Iterator

import pytest

from conftest import (
    BASE_URL,
    http_request,
    render_template,
    start_nginx,
    stop_nginx,
    zstd_decompress,
)

FIXTURE_ROOT = Path("/var/fixtures/empty-body")
EMPTY_HTML = FIXTURE_ROOT / "empty.html"
UPSTREAM_PORT = 9009  # distinct from other test fixture ports (9000-9008)


def _serve_empty_chunked(sock: socket.socket, stop: threading.Event) -> None:
    """Minimal upstream: every request gets a 200 with
    `Transfer-Encoding: chunked` and a zero-chunk terminator only —
    an empty body of *unknown* length (content_length_n == -1 in the
    proxying nginx)."""
    sock.settimeout(0.5)
    while not stop.is_set():
        try:
            c, _ = sock.accept()
        except socket.timeout:
            continue
        try:
            c.settimeout(5)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = c.recv(4096)
                if not chunk:
                    break
                buf += chunk
            c.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/html\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                b"0\r\n\r\n"
            )
        except OSError:
            pass
        finally:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()


@pytest.fixture(scope="module")
def empty_body_nginx() -> Iterator[str]:
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    # 0-byte file served by the core static handler -> known Content-Length: 0,
    # Content-Type: text/html (compressible per zstd_types).
    EMPTY_HTML.write_bytes(b"")

    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", UPSTREAM_PORT))
    sock.listen(8)
    stop = threading.Event()
    thread = threading.Thread(
        target=_serve_empty_chunked, args=(sock, stop), daemon=True
    )
    thread.start()

    extra_locations = f"""
        location = /empty-html {{
            # Restated (template already sets it at http level) so this test
            # is self-contained against template drift — C12-2 only bites
            # under zstd_min_length 0.
            zstd_min_length 0;
            zstd_types text/html;
            alias /var/fixtures/empty-body/empty.html;
            default_type text/html;
        }}
        location = /empty-chunked {{
            # No zstd_min_length here: the directive only applies to known
            # Content-Length responses, never to chunked/unknown-length.
            zstd_types text/html;
            proxy_pass http://127.0.0.1:{UPSTREAM_PORT}/;
            proxy_http_version 1.1;
        }}
"""
    stop_nginx()
    render_template(extra_locations=extra_locations)
    start_nginx()
    try:
        yield BASE_URL
    finally:
        stop_nginx()
        stop.set()
        thread.join(timeout=2)
        sock.close()


def test_known_content_length_zero_not_compressed(empty_body_nginx):
    """Known Content-Length: 0 + zstd_min_length 0: must NOT be compressed.

    The core static handler serves the 0-byte file with a concrete
    Content-Length: 0, so the header filter knows the body is empty before any
    bytes flow. Compressing it is pure waste (empty frame + baseline workspace),
    so the filter must decline regardless of min_length.
    """
    r, body = http_request(empty_body_nginx, "/empty-html", accept_encoding="zstd")
    assert r.status_code == 200, f"status={r.status_code}"
    # Precondition: the response really is a known-length empty body — if the
    # serving setup ever drifts to chunked, this test would silently stop
    # covering the C12-2 path.
    assert r.headers.get("Content-Length") == "0", (
        f"precondition: expected a known Content-Length: 0 response, got "
        f"Content-Length={r.headers.get('Content-Length')!r}"
    )
    assert r.headers.get("Content-Encoding") != "zstd", (
        f"known Content-Length: 0 must not be zstd-encoded, got "
        f"Content-Encoding={r.headers.get('Content-Encoding')!r}"
    )
    assert body == b"", f"empty body must stay empty, got {len(body)} bytes"


def test_empty_chunked_emits_valid_empty_frame(empty_body_nginx):
    """Empty *chunked* (unknown-length) response: compressed by design.

    content_length_n == -1 at header time, so Content-Encoding: zstd is
    committed before the filter can know the body is empty. The body filter
    then sees last_buf on zero input bytes and must emit a valid empty zstd
    frame through the signal-only sentinel-buf path (gzip parity). This is
    the only test exercising that path with a truly empty unknown-length
    body — do not retarget it at a known-CL response.
    """
    r, body = http_request(
        empty_body_nginx, "/empty-chunked", accept_encoding="zstd"
    )
    assert r.status_code == 200, f"status={r.status_code}"
    # Precondition: the upstream response must reach our filter without a
    # Content-Length, otherwise this collapses into the known-CL=0 case.
    assert r.headers.get("Content-Length") is None, (
        f"precondition: expected an unknown-length (chunked) response, got "
        f"Content-Length={r.headers.get('Content-Length')!r}"
    )
    assert r.headers.get("Content-Encoding") == "zstd", (
        f"empty chunked response must be zstd-encoded (encoding committed at "
        f"header time), got "
        f"Content-Encoding={r.headers.get('Content-Encoding')!r}"
    )
    assert body[:4] == b"\x28\xb5\x2f\xfd", (
        f"body missing zstd magic; hex={body[:16].hex()!r}"
    )
    decoded = zstd_decompress(body)
    assert decoded == b"", (
        f"empty chunked body must decompress to empty, got {len(decoded)} bytes"
    )
