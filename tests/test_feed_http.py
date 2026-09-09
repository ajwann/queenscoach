"""`fetch_binary` against a local server, so the byte cap and timeout are real."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from queenscoach.feed_http import FeedFetchError, fetch_binary

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


async def _read_request(reader: asyncio.StreamReader) -> None:
    """Consume the request head, up to and including its blank line."""
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            return


def _responder(head: bytes, body: bytes = b"", *, hang: bool = False) -> Handler:
    """Build a handler that writes a fixed response, optionally never ending it."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request(reader)
        writer.write(head + body)
        await writer.drain()
        if hang:
            # Held open until the fixture tears the connection down, so the
            # client-side cap and timeout are what end the request.
            await asyncio.Event().wait()
        writer.close()

    return handler


@pytest.fixture
async def base_url(request: pytest.FixtureRequest) -> AsyncIterator[str]:
    """Serve one canned response on a loopback port for the duration of a test."""
    handler: Handler = request.param
    connections: set[asyncio.Task[None]] = set()

    async def track(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        connections.add(task)
        try:
            await handler(reader, writer)
        finally:
            connections.discard(task)
            writer.close()

    server = await asyncio.start_server(track, "127.0.0.1", 0)
    port: int = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        for task in list(connections):
            task.cancel()
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    "base_url",
    [_responder(b"HTTP/1.1 200 OK\r\ncontent-length: 3\r\n\r\n", b"\x01\x02\x03")],
    indirect=True,
)
async def test_returns_the_body_bytes_on_success(base_url: str) -> None:
    body = await fetch_binary(base_url, timeout_seconds=5.0, max_bytes=1_000)
    assert body == b"\x01\x02\x03"


@pytest.mark.parametrize(
    "base_url",
    [_responder(b"HTTP/1.1 503 Service Unavailable\r\ncontent-length: 4\r\n\r\n", b"down")],
    indirect=True,
)
async def test_raises_feed_fetch_error_with_the_status_on_a_non_2xx_response(
    base_url: str,
) -> None:
    with pytest.raises(FeedFetchError) as caught:
        await fetch_binary(base_url, timeout_seconds=5.0, max_bytes=1_000)
    assert caught.value.status == 503
    assert caught.value.url == base_url


@pytest.mark.parametrize(
    "base_url",
    [_responder(b"HTTP/1.1 200 OK\r\ncontent-length: 5000\r\n\r\n", b"\x00" * 5_000)],
    indirect=True,
)
async def test_rejects_a_body_that_declares_a_length_over_the_limit(base_url: str) -> None:
    with pytest.raises(FeedFetchError, match="byte limit"):
        await fetch_binary(base_url, timeout_seconds=5.0, max_bytes=100)


@pytest.mark.parametrize(
    "base_url",
    # No content-length, so the cap must be enforced while streaming.
    [_responder(b"HTTP/1.1 200 OK\r\n\r\n", b"\x00" * 65_536, hang=True)],
    indirect=True,
)
async def test_aborts_a_body_that_grows_past_the_limit(base_url: str) -> None:
    with pytest.raises(FeedFetchError, match="exceeded"):
        await fetch_binary(base_url, timeout_seconds=5.0, max_bytes=4_096)


@pytest.mark.parametrize(
    "base_url",
    # Headers sent, body never finishes; the client timeout must fire.
    [_responder(b"HTTP/1.1 200 OK\r\ncontent-length: 100\r\n\r\n", hang=True)],
    indirect=True,
)
async def test_times_out_a_hanging_response(base_url: str) -> None:
    with pytest.raises(FeedFetchError, match="timed out"):
        await fetch_binary(base_url, timeout_seconds=0.2, max_bytes=1_000)


async def test_reports_a_connection_failure_as_a_feed_fetch_error() -> None:
    # Port 1 on loopback is not listening, so the connect fails immediately.
    with pytest.raises(FeedFetchError, match="failed"):
        await fetch_binary("http://127.0.0.1:1/feed.pb", timeout_seconds=2.0, max_bytes=1_000)
