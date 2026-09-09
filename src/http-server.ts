#!/usr/bin/env node
/**
 * HTTP entry point: Streamable HTTP transport for a remotely-hosted server,
 * suitable for use as a Claude custom connector.
 *
 * Runs stateless (no session ids): every request gets its own transport and
 * MCP server instance, while the feed caches live in the shared `Dependencies`.
 * That keeps the process restart-safe and horizontally scalable, at the cost of
 * server-initiated streaming, which these read-only tools never use.
 */

import { createServer as createHttpServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { AuthConfigError, createAuthenticator, type AuthResult } from './auth.ts';
import { ConfigError, loadConfig, type Config } from './config.ts';
import { createRealtimeFeeds } from './realtime.ts';
import { createScheduleLoader } from './static-gtfs.ts';
import { createServer, SERVER_NAME, SERVER_VERSION } from './server.ts';
import type { Dependencies } from './tools.ts';

const MCP_PATH = '/mcp';
const HEALTH_PATH = '/healthz';

/**
 * Splits a request path into the MCP route and an optional secret segment.
 *
 * `/mcp` carries no secret (the token must then arrive in the Authorization
 * header); `/mcp/<token>` carries one in the URL, which is what Claude's
 * URL-only connector dialog needs.
 */
function routeOf(pathname: string): { isMcp: boolean; pathToken: string | undefined } {
  const normalized = pathname.endsWith('/') && pathname.length > 1
    ? pathname.slice(0, -1)
    : pathname;
  if (normalized === MCP_PATH) return { isMcp: true, pathToken: undefined };
  if (normalized.startsWith(`${MCP_PATH}/`)) {
    const segment = normalized.slice(MCP_PATH.length + 1);
    // Only a single segment is a token; deeper paths are not MCP routes.
    if (segment.length > 0 && !segment.includes('/')) {
      return { isMcp: true, pathToken: decodeURIComponent(segment) };
    }
  }
  return { isMcp: false, pathToken: undefined };
}

function log(message: string): void {
  process.stderr.write(`[queenscoach] ${message}\n`);
}

function sendJson(response: ServerResponse, status: number, body: unknown): void {
  const payload = JSON.stringify(body);
  response.writeHead(status, {
    'content-type': 'application/json',
    'content-length': Buffer.byteLength(payload),
  });
  response.end(payload);
}

/** JSON-RPC shaped error, so a protocol client sees a parseable failure. */
function sendRpcError(response: ServerResponse, status: number, message: string): void {
  sendJson(response, status, {
    jsonrpc: '2.0',
    error: { code: status === 404 ? -32601 : -32600, message },
    id: null,
  });
}

/**
 * Reads a request body, refusing anything over `maxBytes`.
 *
 * The cap is enforced while reading rather than after, so an oversized body is
 * abandoned instead of buffered.
 */
async function readBody(request: IncomingMessage, maxBytes: number): Promise<string> {
  const chunks: Buffer[] = [];
  let total = 0;
  for await (const chunk of request) {
    const buffer = chunk as Buffer;
    total += buffer.byteLength;
    if (total > maxBytes) {
      throw new Error(`request body exceeded ${maxBytes} bytes`);
    }
    chunks.push(buffer);
  }
  return Buffer.concat(chunks).toString('utf8');
}

async function handleMcpRequest(
  request: IncomingMessage,
  response: ServerResponse,
  deps: Dependencies,
  config: Config,
): Promise<void> {
  let parsedBody: unknown;
  if (request.method === 'POST') {
    let raw: string;
    try {
      raw = await readBody(request, config.maxBodyBytes);
    } catch (error) {
      sendRpcError(response, 413, error instanceof Error ? error.message : 'Body too large');
      return;
    }
    try {
      parsedBody = JSON.parse(raw);
    } catch {
      sendRpcError(response, 400, 'Request body is not valid JSON');
      return;
    }
  }

  // Stateless: a fresh server and transport per request. The expensive state
  // (schedule and feed caches) is in `deps` and shared across all requests.
  const server = createServer(deps, log);
  // Omitting `sessionIdGenerator` selects stateless mode. It is left out
  // rather than set to `undefined` so the option type stays satisfied under
  // `exactOptionalPropertyTypes`.
  const transport = new StreamableHTTPServerTransport({ enableJsonResponse: true });

  // Tie both lifetimes to the response so nothing leaks if the client hangs up.
  response.on('close', () => {
    void transport.close().catch(() => undefined);
    void server.close().catch(() => undefined);
  });

  try {
    // @ts-expect-error - The SDK's `Transport` declares optional handlers as
    // `onclose?: () => void`, while the transport class exposes them as
    // `(() => void) | undefined`. Those differ only under this project's
    // `exactOptionalPropertyTypes`; the shapes are compatible at runtime.
    await server.connect(transport);
    await transport.handleRequest(request, response, parsedBody);
  } catch (error) {
    log(`request failed: ${error instanceof Error ? error.message : 'unknown error'}`);
    if (!response.headersSent) {
      sendRpcError(response, 500, 'Internal server error');
    } else {
      response.end();
    }
  }
}

async function main(): Promise<void> {
  const config = loadConfig();
  const authenticate = createAuthenticator({
    token: process.env['CATS_AUTH_TOKEN'],
    allowAnonymous: process.env['CATS_ALLOW_ANONYMOUS'] === 'true',
  });
  const anonymous = process.env['CATS_AUTH_TOKEN'] === undefined;

  const deps: Dependencies = {
    loadSchedule: createScheduleLoader(config),
    feeds: createRealtimeFeeds(config),
  };

  const httpServer = createHttpServer((request, response) => {
    const url = new URL(request.url ?? '/', `http://${request.headers.host ?? 'localhost'}`);

    // Unauthenticated: platform health checks run before any secret is known.
    if (url.pathname === HEALTH_PATH) {
      sendJson(response, 200, { status: 'ok', server: SERVER_NAME, version: SERVER_VERSION });
      return;
    }

    const route = routeOf(url.pathname);
    if (!route.isMcp) {
      sendRpcError(response, 404, `Not found. The MCP endpoint is ${MCP_PATH}.`);
      return;
    }

    const result: AuthResult = authenticate({
      authorizationHeader: request.headers.authorization,
      pathToken: route.pathToken,
    });
    if (!result.ok) {
      // Advertise the scheme so a client knows how to authenticate.
      response.setHeader('www-authenticate', 'Bearer');
      sendRpcError(response, result.status, result.message);
      return;
    }

    void handleMcpRequest(request, response, deps, config);
  });

  // Deployed behind a proxy that may hold connections open; keep the server's
  // header timeout above its keep-alive timeout to avoid truncated requests.
  httpServer.keepAliveTimeout = 61_000;
  httpServer.headersTimeout = 65_000;

  await new Promise<void>((resolve, reject) => {
    httpServer.once('error', reject);
    httpServer.listen(config.httpPort, config.httpHost, () => {
      httpServer.off('error', reject);
      resolve();
    });
  });

  log(`listening on http://${config.httpHost}:${config.httpPort}${MCP_PATH}`);
  if (anonymous) {
    log('WARNING: running without authentication (CATS_ALLOW_ANONYMOUS=true)');
  }

  let shuttingDown = false;
  const shutdown = (signal: NodeJS.Signals): void => {
    if (shuttingDown) return;
    shuttingDown = true;
    log(`received ${signal}, draining connections`);
    // Stop accepting work, let in-flight requests finish, then force the rest.
    const forceTimer = setTimeout(() => {
      httpServer.closeAllConnections();
    }, 10_000);
    forceTimer.unref();
    httpServer.close(() => {
      clearTimeout(forceTimer);
      process.exitCode = 0;
    });
  };

  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

main().catch((error: unknown) => {
  if (error instanceof ConfigError || error instanceof AuthConfigError) {
    log(`configuration error: ${error.message}`);
  } else {
    log(`fatal: ${error instanceof Error ? (error.stack ?? error.message) : String(error)}`);
  }
  process.exitCode = 1;
});
