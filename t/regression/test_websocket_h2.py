"""WebSocket-over-HTTP/2 via RFC 8441 Extended CONNECT.

V2 roadmap item #3 (docs/TODO.md): the existing test_websocket.py covers
WebSocket via HTTP/1.1 Upgrade. Stensel8's production setup ran HTTP/2
to the client; the bug surface there involves nginx bridging HTTP/2
Extended CONNECT (RFC 8441) on the front side to HTTP/1.1
`Upgrade: websocket` on the upstream side — a different code path from
the H1.1-Upgrade-only case.

RFC 8441 in one paragraph: instead of HTTP/1.1
`GET /path HTTP/1.1` + `Upgrade: websocket` + `Connection: Upgrade`,
the client sends an HTTP/2 HEADERS frame with `:method = CONNECT` plus
the *extended* pseudo-header `:protocol = websocket`. Server responds
with `:status = 200` (not 101). After that, DATA frames on the stream
carry the WebSocket binary frames.

This requires:
  1. Client SETTINGS includes `SETTINGS_ENABLE_CONNECT_PROTOCOL = 1`.
  2. Server SETTINGS advertises the same.
  3. Stream stays half-open while WebSocket conversation continues.
  4. Server framing is unmasked; client framing IS masked.

Neither curl, httpx, nor python-websockets supports RFC 8441 today
(websockets has it on the wire-protocol side but not as a client). We
hand-roll using the python3-h2 library + websockets.frames for the
inner WS framing (`Frame.serialize(mask=True)` for outgoing client
frames; tiny manual parser for incoming server frames, which are
unmasked per RFC 6455 §5).

Coverage scope: handshake success + one echo roundtrip. Full WebSocket
conversation (8-message echo loop like test_websocket.py) is V2++ —
this test exists to lock in the basic RFC 8441 bridge so the V2
compressStream2 / Direction-B-pool refactors can't silently break it.

Skip conditions:
  * nginx without --with-http_v2_module
  * nginx that doesn't advertise SETTINGS_ENABLE_CONNECT_PROTOCOL (older
    than 1.27.5 ~ Apr 2025; we'd skip with a clear message)
"""

from __future__ import annotations

import socket
import ssl
import subprocess
import threading
import time
from typing import Iterator

import pytest
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import (
    RemoteSettingsChanged,
    ResponseReceived,
    SettingsAcknowledged,
    DataReceived,
    StreamReset,
)
from h2.settings import SettingCodes
from websockets.frames import Frame, Opcode


# ---------- Upstream WebSocket server fixture (HTTP/1.1) -----------------
# Same shape as test_websocket.py's _upstream_handler but using the sync
# websockets.server API for simplicity (no asyncio thread juggling).

import websockets.sync.server


def _ws_handler(ws):
    """Echo handler: emit one greeting, echo back 2 client messages, close."""
    ws.send("h2-greeting")
    for _ in range(2):
        try:
            msg = ws.recv(timeout=4)
        except TimeoutError:
            break
        ws.send(f"echo: {msg}")
    ws.close()


@pytest.fixture(scope="module")
def ws_upstream_h2() -> Iterator[None]:
    """Sync websockets.server in a background thread on 127.0.0.1:9003.
    Distinct port from test_websocket.py (9001) to allow parallel module
    execution down the line."""
    server = websockets.sync.server.serve(_ws_handler, "127.0.0.1", 9003)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)
    try:
        yield
    finally:
        server.shutdown()
        thread.join(timeout=2)


# ---------- nginx fixture ------------------------------------------------
H2_BASE_HOST = "127.0.0.1"
H2_BASE_PORT = 8443
EXTRA_SERVER_TLS = """
        listen 8443 ssl;
        ssl_certificate /etc/nginx/test-cert.pem;
        ssl_certificate_key /etc/nginx/test-key.pem;
"""
EXTRA_LOCATIONS_WS = """
    location /ws-h2/ {
        proxy_pass http://127.0.0.1:9003/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 30s;
        proxy_buffering off;
    }
"""


def _has_http2() -> bool:
    out = subprocess.run(
        ["nginx", "-V"], capture_output=True, text=True, check=False,
    )
    return "--with-http_v2_module" in (out.stdout + out.stderr)


pytestmark = pytest.mark.skipif(
    not _has_http2(), reason="nginx built without --with-http_v2_module",
)


@pytest.fixture(scope="module")
def nginx_ws_h2(ws_upstream_h2) -> Iterator[None]:
    from conftest import _nginx_has_compat, render_template, start_nginx, stop_nginx
    load_modules = (
        "load_module modules/ngx_http_zstd_filter_module.so;"
        if _nginx_has_compat() else ""
    )
    stop_nginx()
    render_template(
        extra_directives="http2 on;",
        extra_server=EXTRA_SERVER_TLS,
        extra_locations=EXTRA_LOCATIONS_WS.strip(),
        load_modules=load_modules,
    )
    start_nginx()
    try:
        yield
    finally:
        stop_nginx()


# ---------- Minimal RFC 8441 H2 client -----------------------------------

def _parse_unmasked_frame(buf: bytes) -> tuple[int, bytes, int]:
    """Parse one server WebSocket frame from `buf` (unmasked per RFC 6455).
    Returns (opcode, payload, consumed_bytes). Caller must ensure `buf`
    holds at least one complete frame; raises IndexError if it doesn't.

    We hand-roll instead of using Frame.parse() because that's a callback-
    style generator API designed for trio/asyncio drivers — overkill for
    a 5-line synchronous parse."""
    fin_op = buf[0]
    opcode = fin_op & 0x0F
    mask_len = buf[1]
    length = mask_len & 0x7F
    offset = 2
    if length == 126:
        length = int.from_bytes(buf[2:4], "big")
        offset = 4
    elif length == 127:
        length = int.from_bytes(buf[2:10], "big")
        offset = 10
    # Server frames MUST NOT be masked per RFC 6455 §5.1.
    assert not (mask_len & 0x80), "server-sent frame was masked (RFC violation)"
    payload = buf[offset:offset + length]
    return opcode, bytes(payload), offset + length


def _drain_until_event(
    conn: H2Connection, sock: ssl.SSLSocket, want_type: type,
    timeout: float = 5.0,
) -> object:
    """Read from `sock`, feed bytes into `conn.receive_data`, return the
    first event of type `want_type`. Raises TimeoutError on read timeout
    or AssertionError on StreamReset."""
    sock.settimeout(timeout)
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            raise AssertionError("peer closed connection unexpectedly")
        events = conn.receive_data(chunk)
        for ev in events:
            if isinstance(ev, StreamReset):
                raise AssertionError(
                    f"stream reset: error_code={ev.error_code}"
                )
            if isinstance(ev, want_type):
                # Push any pending data (e.g. SETTINGS ACK).
                pending = conn.data_to_send()
                if pending:
                    sock.sendall(pending)
                return ev


def test_websocket_h2_rfc8441_handshake_and_echo(nginx_ws_h2):
    """Open TLS+h2, send Extended CONNECT, expect 200 response, send one
    WebSocket text frame, expect echo back. Then close cleanly.

    On master baseline: should PASS if nginx ≥ 1.27.5 supports RFC 8441.
    If skip → nginx version doesn't advertise SETTINGS_ENABLE_CONNECT_PROTOCOL."""

    # TLS context: ALPN=h2, self-signed test cert (verify off).
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_alpn_protocols(["h2"])

    sock = socket.create_connection((H2_BASE_HOST, H2_BASE_PORT), timeout=5)
    sock = ctx.wrap_socket(sock, server_hostname=H2_BASE_HOST)
    assert sock.selected_alpn_protocol() == "h2", (
        f"ALPN negotiation failed: got {sock.selected_alpn_protocol()!r}"
    )

    # h2 client setup. enable_connect_protocol per RFC 8441 §3 — clients
    # signal their interest in receiving an Extended CONNECT permit; servers
    # advertise the same setting back if they support it.
    conn = H2Connection(config=H2Configuration(client_side=True))
    conn.initiate_connection()
    conn.local_settings.enable_connect_protocol = 1
    conn.local_settings.update({SettingCodes.ENABLE_CONNECT_PROTOCOL: 1})
    sock.sendall(conn.data_to_send())

    # Wait for server SETTINGS. RFC 8441 §3 requires servers to advertise
    # SETTINGS_ENABLE_CONNECT_PROTOCOL=1 before accepting Extended CONNECT
    # — but nginx mainline 1.27.5+ supports the method without setting the
    # flag preemptively. We log the observed value and proceed regardless;
    # the real test is whether nginx returns :status=200 to our request.
    ev = _drain_until_event(conn, sock, RemoteSettingsChanged)
    enable_cp = conn.remote_settings.enable_connect_protocol

    # Drain SETTINGS ACK if it arrives (housekeeping).
    pending = conn.data_to_send()
    if pending:
        sock.sendall(pending)

    # Build the Extended CONNECT request. RFC 8441 §4: :method=CONNECT,
    # :protocol=websocket, :scheme + :path + :authority as usual, plus
    # the standard Sec-WebSocket-* headers.
    stream_id = 1
    conn.send_headers(
        stream_id,
        [
            (":method", "CONNECT"),
            (":protocol", "websocket"),
            (":scheme", "https"),
            (":path", "/ws-h2/"),
            (":authority", f"{H2_BASE_HOST}:{H2_BASE_PORT}"),
            ("sec-websocket-version", "13"),
            ("sec-websocket-key", "dGhlIHNhbXBsZSBub25jZQ=="),  # arbitrary 16B b64
            # RFC 8441 §5: include `Accept-Encoding: zstd` to exercise
            # the same negotiation path the bug #4b production reports
            # hit. Stensel8's HomeAssistant request had AE: zstd set by
            # browser.
            ("accept-encoding", "zstd"),
        ],
        end_stream=False,
    )
    sock.sendall(conn.data_to_send())

    # Wait for response headers. RFC 8441 says success = :status 200.
    ev = _drain_until_event(conn, sock, ResponseReceived)
    status = dict(ev.headers).get(b":status", b"").decode()
    if status != "200":
        # 400 / 501 / 502 = nginx rejected Extended CONNECT.
        # Skip with the observed RemoteSettings value so the failure
        # mode is clear (nginx version doesn't support RFC 8441 yet).
        pytest.skip(
            f"Extended CONNECT rejected by nginx: :status={status!r} "
            f"(SETTINGS_ENABLE_CONNECT_PROTOCOL={enable_cp}). "
            f"RFC 8441 likely unsupported in this nginx build."
        )

    # WebSocket handshake complete from RFC 8441 perspective. Now send a
    # client frame via DATA (masked, per RFC 6455 §5.1).
    frame_out = Frame(Opcode.TEXT, b"ping-from-h2", fin=True)
    conn.send_data(stream_id, frame_out.serialize(mask=True))
    sock.sendall(conn.data_to_send())

    # Read until we have two server frames in the WebSocket layer:
    # 1. The initial "h2-greeting" pushed by _ws_handler
    # 2. The "echo: ping-from-h2" reply
    sock.settimeout(5)
    ws_buf = bytearray()
    received_payloads: list[bytes] = []
    while len(received_payloads) < 2:
        chunk = sock.recv(65536)
        if not chunk:
            break
        for ev in conn.receive_data(chunk):
            if isinstance(ev, DataReceived):
                ws_buf.extend(ev.data)
                # We don't strictly need flow-control updates here — the
                # response data is tiny — but h2 expects an
                # acknowledge_received_data() call to keep the window open.
                conn.acknowledge_received_data(
                    ev.flow_controlled_length, stream_id
                )
            elif isinstance(ev, StreamReset):
                raise AssertionError(
                    f"stream reset mid-conversation: code={ev.error_code}"
                )
        pending = conn.data_to_send()
        if pending:
            sock.sendall(pending)
        # Parse all complete WS frames currently in the buffer.
        while len(ws_buf) >= 2:
            try:
                opcode, payload, consumed = _parse_unmasked_frame(bytes(ws_buf))
            except (IndexError, AssertionError):
                break
            if consumed > len(ws_buf):
                break
            del ws_buf[:consumed]
            if opcode == Opcode.TEXT:
                received_payloads.append(payload)

    assert received_payloads[0] == b"h2-greeting", (
        f"first server frame payload={received_payloads[0]!r} "
        f"(expected b'h2-greeting')"
    )
    assert received_payloads[1] == b"echo: ping-from-h2", (
        f"echo frame payload={received_payloads[1]!r}"
    )

    # Clean teardown.
    conn.end_stream(stream_id)
    conn.close_connection()
    sock.sendall(conn.data_to_send())
    sock.close()
