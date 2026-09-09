/**
 * Static GTFS schedule: the lookup tables that give the realtime feeds meaning.
 *
 * The realtime protobufs carry only identifiers (route `29`, stop `02400`,
 * trip `5438306`). This module downloads the published GTFS zip and keeps the
 * three small tables needed to turn those into route names, stop names and
 * coordinates, and trip headsigns. `stop_times.txt` and `shapes.txt` are the
 * bulk of the archive and are deliberately not extracted.
 */

import { unzipSync } from 'fflate';
import type { Config } from './config.ts';
import { parseCsv } from './csv.ts';
import { fetchBinary, FeedFetchError } from './http.ts';
import { TtlCache, type Cached } from './cache.ts';

/** Vehicle mode, derived from GTFS `route_type`. */
export type Mode = 'bus' | 'train';

export interface Route {
  readonly routeId: string;
  /** Rider-facing designator, e.g. `29` or `501`. */
  readonly shortName: string;
  readonly longName: string;
  readonly mode: Mode;
}

export interface Stop {
  readonly stopId: string;
  readonly code: string | undefined;
  readonly name: string;
  readonly latitude: number;
  readonly longitude: number;
}

export interface Trip {
  readonly tripId: string;
  readonly routeId: string;
  readonly headsign: string | undefined;
}

export interface Schedule {
  readonly routes: ReadonlyMap<string, Route>;
  readonly stops: ReadonlyMap<string, Stop>;
  readonly trips: ReadonlyMap<string, Trip>;
}

const ROUTES_FILE = 'routes.txt';
const STOPS_FILE = 'stops.txt';
const TRIPS_FILE = 'trips.txt';
const REQUIRED_FILES: ReadonlySet<string> = new Set([ROUTES_FILE, STOPS_FILE, TRIPS_FILE]);

/**
 * GTFS `route_type` values CATS publishes: 0 (tram/streetcar/light rail) for
 * the Blue and Gold lines, 3 (bus) for everything else. Other rail-ish types
 * are mapped defensively in case the agency adds service.
 */
const RAIL_ROUTE_TYPES: ReadonlySet<string> = new Set(['0', '1', '2', '5', '7', '12']);

function toMode(routeType: string | undefined): Mode {
  return routeType !== undefined && RAIL_ROUTE_TYPES.has(routeType) ? 'train' : 'bus';
}

function toCoordinate(raw: string | undefined): number | undefined {
  if (raw === undefined) return undefined;
  // GTFS files in the wild pad coordinates with spaces; Number() tolerates that.
  const value = Number(raw);
  return Number.isFinite(value) ? value : undefined;
}

function parseSchedule(archive: Uint8Array): Schedule {
  let files: Record<string, Uint8Array>;
  try {
    files = unzipSync(archive, { filter: (file) => REQUIRED_FILES.has(file.name) });
  } catch (cause) {
    throw new Error('Static GTFS archive could not be read as a zip file', { cause });
  }

  const decoder = new TextDecoder('utf-8');
  const read = (name: string): Array<Record<string, string | undefined>> => {
    const bytes = files[name];
    if (bytes === undefined) {
      throw new Error(`Static GTFS archive is missing ${name}`);
    }
    return parseCsv(decoder.decode(bytes));
  };

  const routes = new Map<string, Route>();
  for (const row of read(ROUTES_FILE)) {
    const routeId = row['route_id'];
    if (routeId === undefined) continue;
    const shortName = row['route_short_name'] ?? routeId;
    routes.set(routeId, {
      routeId,
      shortName,
      longName: row['route_long_name'] ?? shortName,
      mode: toMode(row['route_type']),
    });
  }

  const stops = new Map<string, Stop>();
  for (const row of read(STOPS_FILE)) {
    const stopId = row['stop_id'];
    const latitude = toCoordinate(row['stop_lat']);
    const longitude = toCoordinate(row['stop_lon']);
    // A stop without coordinates cannot answer a location question; skip it.
    if (stopId === undefined || latitude === undefined || longitude === undefined) continue;
    stops.set(stopId, {
      stopId,
      code: row['stop_code'],
      name: row['stop_name'] ?? stopId,
      latitude,
      longitude,
    });
  }

  const trips = new Map<string, Trip>();
  for (const row of read(TRIPS_FILE)) {
    const tripId = row['trip_id'];
    const routeId = row['route_id'];
    if (tripId === undefined || routeId === undefined) continue;
    trips.set(tripId, { tripId, routeId, headsign: row['trip_headsign'] });
  }

  if (routes.size === 0 || stops.size === 0) {
    throw new Error('Static GTFS archive parsed but contained no routes or stops');
  }
  return { routes, stops, trips };
}

/** Loads and caches the static schedule for the configured feed. */
export function createScheduleLoader(config: Config): () => Promise<Cached<Schedule>> {
  const cache = new TtlCache<Schedule>(config.staticTtlMs, async () => {
    const archive = await fetchBinary(config.staticGtfsUrl, {
      timeoutMs: config.requestTimeoutMs,
      maxBytes: config.maxStaticBytes,
    });
    return parseSchedule(archive);
  });
  return () => cache.get();
}

export { parseSchedule, FeedFetchError };
