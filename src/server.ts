/**
 * MCP server definition: the tool registrations shared by every transport.
 *
 * Transport-agnostic on purpose — `index.ts` binds this to stdio for local
 * use, `http-server.ts` binds it to Streamable HTTP for remote use.
 */

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import {
  findVehicle,
  findVehicleInput,
  getArrivals,
  getArrivalsInput,
  listVehicles,
  listVehiclesInput,
  type Dependencies,
  type ToolResult,
} from './tools.ts';

export const SERVER_NAME = 'queenscoach';
export const SERVER_VERSION = '1.0.0';

const INSTRUCTIONS =
  'Live Charlotte Area Transit System (CATS) bus and light rail data. ' +
  'Use find_vehicle to locate one bus/train or a whole route, list_vehicles ' +
  'for a system-wide position snapshot, and get_arrivals for predicted ' +
  'arrival times at a stop. Coordinates are WGS84 decimal degrees and ' +
  'times are ISO 8601 UTC.';

/**
 * Runs a tool and renders its payload.
 *
 * Feed failures are reported as tool errors rather than thrown, so the model
 * can retry or explain instead of the whole call failing opaquely. Only the
 * message is surfaced; stack traces stay in the server log.
 */
async function respond(name: string, run: () => Promise<ToolResult>, log: (message: string) => void) {
  try {
    const payload = await run();
    return {
      content: [{ type: 'text' as const, text: JSON.stringify(payload, null, 2) }],
      structuredContent: payload,
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Unknown error';
    log(`tool ${name} failed: ${message}`);
    return {
      isError: true,
      content: [{ type: 'text' as const, text: `CATS feed request failed: ${message}` }],
    };
  }
}

export function createServer(deps: Dependencies, log: (message: string) => void): McpServer {
  const server = new McpServer(
    { name: SERVER_NAME, version: SERVER_VERSION },
    { instructions: INSTRUCTIONS },
  );

  server.registerTool(
    'find_vehicle',
    {
      title: 'Find a bus or train',
      description:
        'Locate a specific CATS bus or train and return its current GPS coordinates. ' +
        'Give "vehicle" for a vehicle number (e.g. "2301"), or "route" to get every ' +
        'vehicle currently running a route (e.g. "9", "501", "Blue Line"). Includes ' +
        'heading, speed, occupancy, and next scheduled stop when available.',
      inputSchema: findVehicleInput,
      annotations: { readOnlyHint: true, openWorldHint: true },
    },
    async (input) => respond('find_vehicle', () => findVehicle(deps, input), log),
  );

  server.registerTool(
    'list_vehicles',
    {
      title: 'List all vehicle positions',
      description:
        'Return the current GPS coordinates of every CATS bus and train in service. ' +
        'Optionally filter to buses or trains, or to a single route.',
      inputSchema: listVehiclesInput,
      annotations: { readOnlyHint: true, openWorldHint: true },
    },
    async (input) => respond('list_vehicles', () => listVehicles(deps, input), log),
  );

  server.registerTool(
    'get_arrivals',
    {
      title: 'Get arrival times at a stop',
      description:
        'Estimated arrival times of buses or trains at a specific stop or station. ' +
        'Accepts a stop id, stop code, or part of a stop name. Reports minutes away, ' +
        'schedule deviation, the vehicle number, and any service alerts for that stop.',
      inputSchema: getArrivalsInput,
      annotations: { readOnlyHint: true, openWorldHint: true },
    },
    async (input) => respond('get_arrivals', () => getArrivals(deps, input), log),
  );

  return server;
}
