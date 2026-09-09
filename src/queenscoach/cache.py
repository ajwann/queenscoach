"""TTL cache with single-flight refresh, shared by the feed loaders."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Cached(Generic[T]):
    """A loaded value together with the wall-clock time it was loaded."""

    value: T
    #: Unix time, in seconds, when the underlying data was fetched.
    fetched_at: float


class TtlCache(Generic[T]):
    """Reuse the result of ``load`` until ``ttl_seconds`` elapses.

    Concurrent callers share one in-flight load. That load is not cancelled
    when a caller is: one caller giving up must not abort a fetch the others
    are still awaiting, so callers await it through :func:`asyncio.shield` and
    the load bounds itself with its own timeout instead.

    A failed load is not cached, but a stale entry is retained and returned so
    a transient feed outage degrades to slightly old data rather than an error.
    """

    def __init__(self, ttl_seconds: float, load: Callable[[], Awaitable[T]]) -> None:
        self._ttl_seconds = ttl_seconds
        self._load = load
        self._entry: Cached[T] | None = None
        #: Monotonic reading paired with ``_entry``; TTL must not follow a clock jump.
        self._loaded_at = 0.0
        self._in_flight: asyncio.Task[Cached[T]] | None = None
        self._lock = asyncio.Lock()

    def _fresh(self) -> Cached[T] | None:
        entry = self._entry
        if entry is not None and time.monotonic() - self._loaded_at < self._ttl_seconds:
            return entry
        return None

    async def get(self) -> Cached[T]:
        """Return the cached value, refreshing it if it has expired.

        Raises:
            Exception: whatever ``load`` raised, but only when no previously
                loaded value is available to serve instead.
        """
        fresh = self._fresh()
        if fresh is not None:
            return fresh

        async with self._lock:
            # Another caller may have refreshed while we waited for the lock.
            fresh = self._fresh()
            if fresh is not None:
                return fresh
            if self._in_flight is None or self._in_flight.done():
                self._in_flight = asyncio.create_task(self._refresh())
            task = self._in_flight

        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            stale = self._entry
            if stale is not None:
                return stale
            raise

    async def _refresh(self) -> Cached[T]:
        entry = Cached(value=await self._load(), fetched_at=time.time())
        self._entry = entry
        self._loaded_at = time.monotonic()
        return entry
