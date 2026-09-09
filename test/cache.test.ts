import { test } from 'node:test';
import assert from 'node:assert/strict';
import { TtlCache } from '../src/cache.ts';

test('reuses a value within the TTL', async () => {
  let calls = 0;
  const cache = new TtlCache(60_000, async () => {
    calls += 1;
    return calls;
  });
  assert.equal((await cache.get()).value, 1);
  assert.equal((await cache.get()).value, 1);
  assert.equal(calls, 1);
});

test('concurrent callers share one in-flight load', async () => {
  let calls = 0;
  const cache = new TtlCache(60_000, async () => {
    calls += 1;
    await new Promise((resolve) => setTimeout(resolve, 5));
    return calls;
  });
  const results = await Promise.all([cache.get(), cache.get(), cache.get()]);
  assert.equal(calls, 1);
  for (const result of results) assert.equal(result.value, 1);
});

test('refetches once the TTL has elapsed', async () => {
  let calls = 0;
  const cache = new TtlCache(1, async () => {
    calls += 1;
    return calls;
  });
  assert.equal((await cache.get()).value, 1);
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal((await cache.get()).value, 2);
});

test('serves stale data when a refresh fails', async () => {
  let calls = 0;
  const cache = new TtlCache(1, async () => {
    calls += 1;
    if (calls > 1) throw new Error('feed down');
    return 'first';
  });
  assert.equal((await cache.get()).value, 'first');
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal((await cache.get()).value, 'first');
});

test('propagates the failure when there is nothing cached yet', async () => {
  const cache = new TtlCache(1000, async () => {
    throw new Error('feed down');
  });
  await assert.rejects(cache.get(), /feed down/);
});

test('recovers on a later attempt after a failed first load', async () => {
  let calls = 0;
  const cache = new TtlCache(1, async () => {
    calls += 1;
    if (calls === 1) throw new Error('feed down');
    return 'ok';
  });
  await assert.rejects(cache.get());
  assert.equal((await cache.get()).value, 'ok');
});
