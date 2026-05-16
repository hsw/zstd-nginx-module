"""WebSocket-via-nginx-with-zstd regression test (RFC 6455).

Targets the Stensel8 / lowkeypriority production scenario from PR #23
thread: HomeAssistant proxied via `proxy_pass` + `Connection: Upgrade` +
`zstd on` + `proxy_buffering off` → worker freeze.

The existing `test_proxy_flush[upgrade]` sub-test is a stand-in (200 OK
+ buffered body, not a real Upgrade handshake). This test does the full
protocol: client sends HTTP/1.1 Upgrade through nginx → nginx forwards to
upstream → upstream returns 101 Switching Protocols → bidirectional
WebSocket frames roundtrip.

What we assert:
  1. Handshake completes within 5 s (no freeze on the Upgrade response).
  2. Server-pushed frames arrive at the client (no body_filter eating
     bytes after Switching Protocols).
  3. Client → server echo works.
  4. Clean close from both sides.

What this does NOT cover (V2 axes):
  * permessage-deflate or permessage-zstd extension negotiation
  * HTTP/2 to client + WebSocket-over-HTTP2 (RFC 8441)
  * HTTP/3 / QUIC (Stensel8's full setup)

Uses python3-websockets (apt-installed, version 10.4 on ubuntu-24.04+)
for client AND upstream server framing — saves ~100 LoC of hand-rolled
RFC 6455 plumbing.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Iterator

import pytest
import websockets

from conftest import BASE_URL, render_template, start_nginx, stop_nginx

UPSTREAM_PORT = 9001
NGINX_PORT = 8080

EXTRA_LOCATIONS = """
    # WebSocket-Upgrade location. zstd is `on` from the http context
    # default — we want to exercise the filter under Upgrade semantics
    # (Stensel8's exact config shape).
    location /ws/ {
        proxy_pass http://127.0.0.1:9001/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 30s;
        proxy_buffering off;
    }
"""


async def _upstream_handler(ws):
    """Echo handler — receive frames, echo each back. After 4 echoes the
    server closes cleanly. Also sends one unsolicited frame at start to
    test the server→client direction independently of the echo path."""
    await ws.send("hello-from-upstream")
    echoes = 0
    async for message in ws:
        await ws.send(f"echo: {message}")
        echoes += 1
        if echoes >= 4:
            break
    await ws.close()


async def _upstream_serve(stop_event: asyncio.Event):
    async with websockets.serve(_upstream_handler, "127.0.0.1", UPSTREAM_PORT):
        await stop_event.wait()


def _run_upstream(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event):
    """Thread entry point: run the asyncio upstream server on its own loop."""
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_upstream_serve(stop_event))


@pytest.fixture(scope="module")
def ws_upstream() -> Iterator[None]:
    """Start the asyncio WebSocket upstream server in a background thread."""
    loop = asyncio.new_event_loop()
    stop_event = asyncio.Event()
    thread = threading.Thread(
        target=_run_upstream, args=(loop, stop_event), daemon=True
    )
    thread.start()
    # Give the server a beat to bind. websockets.serve is async — there's
    # no simple "ready" hook, but bind happens during enter of the async
    # context manager. 100 ms is plenty for localhost.
    time.sleep(0.1)
    try:
        yield
    finally:
        loop.call_soon_threadsafe(stop_event.set)
        thread.join(timeout=2)
        loop.close()


@pytest.fixture(scope="module")
def nginx_with_ws(ws_upstream) -> Iterator[str]:
    """Module-scoped nginx with the /ws/ proxy location."""
    stop_nginx()
    render_template(extra_locations=EXTRA_LOCATIONS.strip())
    start_nginx()
    try:
        yield BASE_URL
    finally:
        stop_nginx()


def test_websocket_through_zstd_filter(nginx_with_ws):
    """Full WebSocket handshake + bidirectional traffic through an nginx
    location that has `zstd on` and `proxy_buffering off`. Asserts no
    freeze (5-second hard timeout) and that all expected frames arrive.

    On master baseline this is the Stensel8 production scenario; if the
    Upgrade response gets caught by zstd's body_filter (filter activated
    on the 101 response's zero-byte body, sentinel-buf path), the worker
    can freeze and the handshake never completes."""
    url = nginx_with_ws.replace("http://", "ws://") + "/ws/"

    async def run_client():
        # Apply a hard timeout to the whole conversation — freeze symptom
        # is "client never sees frames", which an async timeout catches.
        async with asyncio.timeout(5):
            async with websockets.connect(url) as ws:
                # 1. Server-pushed initial message (independent of echo).
                first = await ws.recv()
                assert first == "hello-from-upstream", (
                    f"unexpected server greeting: {first!r}"
                )

                # 2. Send 4 messages, expect 4 echoes back. Each "echo: X"
                # roundtrip exercises both client→server frame parsing
                # and server→client frame emission through nginx.
                sent_msgs = ["msg-1", "msg-2", "msg-3", "msg-4"]
                received = []
                for m in sent_msgs:
                    await ws.send(m)
                    reply = await ws.recv()
                    received.append(reply)

                assert received == [f"echo: {m}" for m in sent_msgs], (
                    f"echo roundtrip mismatch: sent={sent_msgs} received={received}"
                )

    asyncio.run(run_client())


def test_websocket_handshake_speed(nginx_with_ws):
    """Smoke check: handshake (Upgrade negotiation) must complete in well
    under a second. Stensel8's freeze appears to manifest at the
    handshake / first-frame boundary; this is a cheaper canary than the
    full echo test above."""
    url = nginx_with_ws.replace("http://", "ws://") + "/ws/"

    async def run():
        start = time.monotonic()
        async with asyncio.timeout(3):
            async with websockets.connect(url) as ws:
                handshake_ms = (time.monotonic() - start) * 1000.0
                # Close immediately — we only care about how fast the
                # 101 response came back.
                await ws.close()
        return handshake_ms

    elapsed_ms = asyncio.run(run())
    assert elapsed_ms < 1000, (
        f"WebSocket handshake took {elapsed_ms:.0f}ms — flush-promotion "
        f"gap on the 101 Switching Protocols response (zero-body) "
        f"may be holding it"
    )
