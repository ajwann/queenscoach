#!/usr/bin/env node
/**
 * stdio entry point: for a locally-launched server (Claude Desktop, Claude Code).
 *
 * stdout carries protocol traffic only; all diagnostics go to stderr.
 * For a remotely-hosted server, use `http-server.ts` instead.
 */

import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { ConfigError, loadConfig } from './config.ts';
import { createScheduleLoader } from './static-gtfs.ts';
import { createRealtimeFeeds } from './realtime.ts';
import { createServer } from './server.ts';
import type { Dependencies } from './tools.ts';

function log(message: string): void {
  process.stderr.write(`[queenscoach] ${message}\n`);
}

async function main(): Promise<void> {
  const config = loadConfig();
  const deps: Dependencies = {
    loadSchedule: createScheduleLoader(config),
    feeds: createRealtimeFeeds(config),
  };

  const server = createServer(deps, log);
  await server.connect(new StdioServerTransport());
  log('ready on stdio');

  let shuttingDown = false;
  const shutdown = (signal: NodeJS.Signals): void => {
    if (shuttingDown) return;
    shuttingDown = true;
    log(`received ${signal}, shutting down`);
    server
      .close()
      .catch((error: unknown) => {
        log(`error during shutdown: ${error instanceof Error ? error.message : 'unknown'}`);
      })
      .finally(() => {
        process.exitCode = 0;
      });
  };

  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

main().catch((error: unknown) => {
  if (error instanceof ConfigError) {
    log(`configuration error: ${error.message}`);
  } else {
    log(`fatal: ${error instanceof Error ? (error.stack ?? error.message) : String(error)}`);
  }
  process.exitCode = 1;
});
