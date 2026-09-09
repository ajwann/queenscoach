/** The three CATS tools exposed over MCP. */

import { z } from 'zod';
import type { Cached } from './cache.ts';
import type { Mode, Schedule } from './static-gtfs.ts';
import type { RealtimeFeeds } from './realtime.ts';
import {
  alertsFor,
  arrivalsAtStop,
  distanceMeters,
  findRoutes,
  findStops,
  matchesVehicleQuery,
  toVehicleViews,
  type TransitSnapshot,
  type VehicleView,
} from './transit.ts';

/** Keeps a single tool response well under typical model context limits. */
const MAX_RESULTS = 250;

const modeSchema = z.enum(['bus', 'train']);

export const findVehicleInput = {
  vehicle: z
    .string()
    .min(1)
    .max(64)
    .optional()
    .describe('Vehicle number as shown on the bus or train, e.g. "2301".'),
  route: z
    .string()
    .min(1)
    .max(64)
    .optional()
    .describe('Route to locate, e.g. "9", "501", or "Blue Line". Returns every vehicle on it.'),
  mode: modeSchema.optional().describe('Restrict results to buses or trains.'),
};

export const listVehiclesInput = {
  mode: modeSchema.optional().describe('Restrict results to buses or trains.'),
  route: z.string().min(1).max(64).optional().describe('Restrict results to one route.'),
  limit: z.number().int().min(1).max(MAX_RESULTS).optional().describe('Maximum vehicles to return.'),
};

export const getArrivalsInput = {
  stop: z
    .string()
    .min(1)
    .max(128)
    .describe('Stop id, stop code, or part of a stop name, e.g. "02400" or "CTC Station".'),
  route: z.string().min(1).max(64).optional().describe('Only show arrivals for this route.'),
  mode: modeSchema.optional().describe('Only show bus or train arrivals.'),
  limit: z.number().int().min(1).max(50).optional().describe('Maximum arrivals to return.'),
};

export interface FindVehicleInput {
  readonly vehicle?: string | undefined;
  readonly route?: string | undefined;
  readonly mode?: Mode | undefined;
}

export interface ListVehiclesInput {
  readonly mode?: Mode | undefined;
  readonly route?: string | undefined;
  readonly limit?: number | undefined;
}

export interface GetArrivalsInput {
  readonly stop: string;
  readonly route?: string | undefined;
  readonly mode?: Mode | undefined;
  readonly limit?: number | undefined;
}

export interface Dependencies {
  readonly loadSchedule: () => Promise<Cached<Schedule>>;
  readonly feeds: RealtimeFeeds;
  readonly now?: () => number;
}

/** Structured payload returned to the model; also rendered as JSON text. */
export type ToolResult = Record<string, unknown>;

async function snapshot(deps: Dependencies): Promise<TransitSnapshot & { feedAgeSeconds: number }> {
  const [schedule, vehicles, tripUpdates] = await Promise.all([
    deps.loadSchedule(),
    deps.feeds.vehiclePositions(),
    deps.feeds.tripUpdates(),
  ]);
  const nowMs = (deps.now ?? Date.now)();
  return {
    schedule: schedule.value,
    vehicles: vehicles.value,
    tripUpdates: tripUpdates.value,
    nowMs,
    feedAgeSeconds: Math.max(0, Math.round((nowMs - vehicles.fetchedAt) / 1000)),
  };
}

function applyMode(views: readonly VehicleView[], mode: Mode | undefined): VehicleView[] {
  return mode === undefined ? [...views] : views.filter((view) => view.mode === mode);
}

export async function findVehicle(
  deps: Dependencies,
  input: FindVehicleInput,
): Promise<ToolResult> {
  if (input.vehicle === undefined && input.route === undefined) {
    return {
      error: 'Provide "vehicle" (a vehicle number) or "route" to locate.',
      hint: 'Use list_vehicles to see everything currently in service.',
    };
  }

  const state = await snapshot(deps);
  let candidates = state.vehicles;

  if (input.vehicle !== undefined) {
    const query = input.vehicle;
    candidates = candidates.filter((vehicle) => matchesVehicleQuery(vehicle, query));
  }

  let matchedRoutes: string[] | undefined;
  if (input.route !== undefined) {
    const routes = findRoutes(state.schedule, input.route);
    if (routes.length === 0) {
      return {
        error: `No route matched ${JSON.stringify(input.route)}.`,
        availableRoutes: [...state.schedule.routes.values()]
          .map((route) => route.shortName)
          .sort((a, b) => a.localeCompare(b, undefined, { numeric: true })),
      };
    }
    matchedRoutes = routes.map((route) => route.shortName);
    const routeIds = new Set(routes.map((route) => route.routeId));
    candidates = candidates.filter(
      (vehicle) =>
        (vehicle.routeId !== undefined && routeIds.has(vehicle.routeId)) ||
        (vehicle.tripId !== undefined &&
          routeIds.has(state.schedule.trips.get(vehicle.tripId)?.routeId ?? '')),
    );
  }

  const views = applyMode(toVehicleViews(candidates, state), input.mode);

  if (views.length === 0) {
    return {
      matches: 0,
      message:
        input.vehicle !== undefined
          ? `Vehicle ${input.vehicle} is not reporting a position right now. Vehicles appear only while in service.`
          : `No vehicles are currently in service on ${matchedRoutes?.join(', ') ?? 'that route'}.`,
      ...(matchedRoutes === undefined ? {} : { matchedRoutes }),
      feedAgeSeconds: state.feedAgeSeconds,
    };
  }

  return {
    matches: views.length,
    ...(matchedRoutes === undefined ? {} : { matchedRoutes }),
    vehicles: views.slice(0, MAX_RESULTS),
    feedAgeSeconds: state.feedAgeSeconds,
  };
}

export async function listVehicles(
  deps: Dependencies,
  input: ListVehiclesInput,
): Promise<ToolResult> {
  const state = await snapshot(deps);
  let candidates = state.vehicles;
  let matchedRoutes: string[] | undefined;

  if (input.route !== undefined) {
    const routes = findRoutes(state.schedule, input.route);
    if (routes.length === 0) {
      return { error: `No route matched ${JSON.stringify(input.route)}.` };
    }
    matchedRoutes = routes.map((route) => route.shortName);
    const routeIds = new Set(routes.map((route) => route.routeId));
    candidates = candidates.filter(
      (vehicle) => vehicle.routeId !== undefined && routeIds.has(vehicle.routeId),
    );
  }

  const views = applyMode(toVehicleViews(candidates, state), input.mode).sort(
    (a, b) =>
      (a.route?.name ?? '').localeCompare(b.route?.name ?? '', undefined, { numeric: true }) ||
      a.vehicle.localeCompare(b.vehicle, undefined, { numeric: true }),
  );

  const limit = Math.min(input.limit ?? MAX_RESULTS, MAX_RESULTS);
  const counts = { bus: 0, train: 0, unknown: 0 };
  for (const view of views) counts[view.mode] += 1;

  return {
    totalInService: views.length,
    returned: Math.min(views.length, limit),
    countsByMode: counts,
    ...(matchedRoutes === undefined ? {} : { matchedRoutes }),
    vehicles: views.slice(0, limit),
    feedAgeSeconds: state.feedAgeSeconds,
  };
}

export async function getArrivals(
  deps: Dependencies,
  input: GetArrivalsInput,
): Promise<ToolResult> {
  const state = await snapshot(deps);
  const stops = findStops(state.schedule, input.stop);

  if (stops.length === 0) {
    return {
      error: `No stop matched ${JSON.stringify(input.stop)}.`,
      hint: 'Try a stop id like "02400", or part of a stop name like "Beatties Ford".',
    };
  }

  const stop = stops[0];
  if (stop === undefined) {
    return { error: `No stop matched ${JSON.stringify(input.stop)}.` };
  }

  let routeIds: Set<string> | undefined;
  let matchedRoutes: string[] | undefined;
  if (input.route !== undefined) {
    const routes = findRoutes(state.schedule, input.route);
    if (routes.length === 0) {
      return { error: `No route matched ${JSON.stringify(input.route)}.` };
    }
    matchedRoutes = routes.map((route) => route.shortName);
    routeIds = new Set(routes.map((route) => route.routeId));
  }

  const arrivals = arrivalsAtStop(stop, state, {
    ...(routeIds === undefined ? {} : { routeIds }),
    ...(input.mode === undefined ? {} : { mode: input.mode }),
  });

  const limit = Math.min(input.limit ?? 10, 50);
  const alertRouteIds = new Set(
    arrivals.map((arrival) => arrival.route?.routeId).filter((id): id is string => id !== undefined),
  );
  const alerts = await deps.feeds
    .alerts()
    .then((cached) => alertsFor(cached.value, stop.stopId, alertRouteIds))
    // Alerts are supplementary; arrivals stay useful if that one feed is down.
    .catch(() => []);

  const otherMatches = stops.slice(1, 6).map((candidate) => ({
    stopId: candidate.stopId,
    name: candidate.name,
    metersAway: Math.round(
      distanceMeters(stop.latitude, stop.longitude, candidate.latitude, candidate.longitude),
    ),
  }));

  return {
    stop: {
      stopId: stop.stopId,
      name: stop.name,
      latitude: stop.latitude,
      longitude: stop.longitude,
    },
    ...(otherMatches.length > 0 ? { otherStopsMatchingQuery: otherMatches } : {}),
    ...(matchedRoutes === undefined ? {} : { matchedRoutes }),
    arrivalCount: arrivals.length,
    arrivals: arrivals.slice(0, limit),
    ...(arrivals.length === 0
      ? { message: 'No realtime arrivals are predicted for this stop right now.' }
      : {}),
    ...(alerts.length > 0 ? { serviceAlerts: alerts } : {}),
    feedAgeSeconds: state.feedAgeSeconds,
  };
}
