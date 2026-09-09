from __future__ import annotations

import asyncio

import pytest

from queenscoach.cache import TtlCache


async def test_reuses_a_value_within_the_ttl() -> None:
    calls = 0

    async def load() -> int:
        nonlocal calls
        calls += 1
        return calls

    cache: TtlCache[int] = TtlCache(60.0, load)
    assert (await cache.get()).value == 1
    assert (await cache.get()).value == 1
    assert calls == 1


async def test_concurrent_callers_share_one_in_flight_load() -> None:
    calls = 0

    async def load() -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.005)
        return calls

    cache: TtlCache[int] = TtlCache(60.0, load)
    results = await asyncio.gather(cache.get(), cache.get(), cache.get())
    assert calls == 1
    assert [result.value for result in results] == [1, 1, 1]


async def test_refetches_once_the_ttl_has_elapsed() -> None:
    calls = 0

    async def load() -> int:
        nonlocal calls
        calls += 1
        return calls

    cache: TtlCache[int] = TtlCache(0.001, load)
    assert (await cache.get()).value == 1
    await asyncio.sleep(0.005)
    assert (await cache.get()).value == 2


async def test_serves_stale_data_when_a_refresh_fails() -> None:
    calls = 0

    async def load() -> str:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("feed down")
        return "first"

    cache: TtlCache[str] = TtlCache(0.001, load)
    assert (await cache.get()).value == "first"
    await asyncio.sleep(0.005)
    assert (await cache.get()).value == "first"


async def test_propagates_the_failure_when_there_is_nothing_cached_yet() -> None:
    async def load() -> str:
        raise RuntimeError("feed down")

    cache: TtlCache[str] = TtlCache(60.0, load)
    with pytest.raises(RuntimeError, match="feed down"):
        await cache.get()


async def test_recovers_on_a_later_attempt_after_a_failed_first_load() -> None:
    calls = 0

    async def load() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("feed down")
        return "ok"

    cache: TtlCache[str] = TtlCache(0.001, load)
    with pytest.raises(RuntimeError):
        await cache.get()
    assert (await cache.get()).value == "ok"


async def test_one_caller_cancelling_does_not_abort_the_shared_load() -> None:
    started = asyncio.Event()

    async def load() -> str:
        started.set()
        await asyncio.sleep(0.05)
        return "loaded"

    cache: TtlCache[str] = TtlCache(60.0, load)
    giving_up = asyncio.create_task(cache.get())
    waiting = asyncio.create_task(cache.get())
    await started.wait()
    giving_up.cancel()

    with pytest.raises(asyncio.CancelledError):
        await giving_up
    assert (await waiting).value == "loaded"
