"""Bounded HTTP fetch helper shared by the static and realtime loaders."""

from __future__ import annotations

import httpx

_ACCEPT = "application/octet-stream, application/x-zip-compressed, */*"


class FeedFetchError(Exception):
    """A feed could not be retrieved."""

    def __init__(self, message: str, url: str, status: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status = status


async def fetch_binary(url: str, *, timeout_seconds: float, max_bytes: int) -> bytes:
    """Fetch ``url`` into memory with an explicit timeout and a hard byte ceiling.

    The body is streamed so an oversized or endless response is abandoned as
    soon as it crosses ``max_bytes`` rather than after buffering it.

    Raises:
        FeedFetchError: on a transport failure, a timeout, a non-2xx status, or
            a body over ``max_bytes``.
    """
    try:
        async with (
            httpx.AsyncClient(
                timeout=timeout_seconds, follow_redirects=True, headers={"accept": _ACCEPT}
            ) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code >= 400:
                raise FeedFetchError(
                    f"Request to {url} returned HTTP {response.status_code} "
                    f"{response.reason_phrase}".strip(),
                    url,
                    status=response.status_code,
                )

            declared = response.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                raise FeedFetchError(
                    f"Response from {url} declares {declared} bytes, "
                    f"over the {max_bytes} byte limit",
                    url,
                )

            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise FeedFetchError(
                        f"Response from {url} exceeded the {max_bytes} byte limit", url
                    )
                chunks.append(chunk)
    except httpx.TimeoutException as error:
        raise FeedFetchError(f"Request to {url} failed: timed out", url) from error
    except httpx.HTTPError as error:
        raise FeedFetchError(f"Request to {url} failed: {error}", url) from error

    return b"".join(chunks)
