import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer, type Server } from 'node:http';
import { once } from 'node:events';
import type { AddressInfo } from 'node:net';
import { fetchBinary, FeedFetchError } from '../src/http.ts';

async function withServer(
  handler: Parameters<typeof createServer>[1],
  run: (baseUrl: string) => Promise<void>,
): Promise<void> {
  const server: Server = createServer(handler);
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const { port } = server.address() as AddressInfo;
  try {
    await run(`http://127.0.0.1:${port}`);
  } finally {
    server.close();
    await once(server, 'close');
  }
}

test('returns the body bytes on success', async () => {
  await withServer(
    (_request, response) => response.end(Buffer.from([1, 2, 3])),
    async (baseUrl) => {
      const bytes = await fetchBinary(baseUrl, { timeoutMs: 5_000, maxBytes: 1_000 });
      assert.deepEqual([...bytes], [1, 2, 3]);
    },
  );
});

test('raises FeedFetchError with the status on a non-2xx response', async () => {
  await withServer(
    (_request, response) => {
      response.statusCode = 503;
      response.end('down');
    },
    async (baseUrl) => {
      await assert.rejects(
        fetchBinary(baseUrl, { timeoutMs: 5_000, maxBytes: 1_000 }),
        (error: unknown) => error instanceof FeedFetchError && error.status === 503,
      );
    },
  );
});

test('rejects a body that declares a length over the limit', async () => {
  await withServer(
    (_request, response) => response.end(Buffer.alloc(5_000)),
    async (baseUrl) => {
      await assert.rejects(
        fetchBinary(baseUrl, { timeoutMs: 5_000, maxBytes: 100 }),
        /byte limit/,
      );
    },
  );
});

test('aborts a chunked body that grows past the limit', async () => {
  await withServer(
    (_request, response) => {
      // No content-length, so the cap must be enforced while streaming.
      response.writeHead(200, { 'transfer-encoding': 'chunked' });
      const chunk = Buffer.alloc(1024);
      const timer = setInterval(() => response.write(chunk), 1);
      response.on('close', () => clearInterval(timer));
    },
    async (baseUrl) => {
      await assert.rejects(
        fetchBinary(baseUrl, { timeoutMs: 5_000, maxBytes: 4096 }),
        (error: unknown) => error instanceof FeedFetchError && /exceeded/.test(error.message),
      );
    },
  );
});

test('times out a hanging response without leaking the request', async () => {
  await withServer(
    (_request, response) => {
      response.writeHead(200);
      // Never finishes; the client timeout must fire.
    },
    async (baseUrl) => {
      await assert.rejects(
        fetchBinary(baseUrl, { timeoutMs: 60, maxBytes: 1_000 }),
        (error: unknown) => error instanceof FeedFetchError && /timed out/.test(error.message),
      );
    },
  );
});
