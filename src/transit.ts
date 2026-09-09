/**
 * Domain layer: joins the realtime feeds to the static schedule and resolves
 * the human-friendly queries the tools accept ("9", "Blue Line", "CTC").
 */

import type { Mode, Route, Schedule, Stop } from './static-gtfs.ts';
import type { ServiceAlert, TripUpdate, VehiclePosition } from './realtime.ts';

export interface ResolvedRouteRef {
  readonly routeId: string;
  readonly name: string;
  readonly longName: string;
  readonly mode: Mode;
}

export interface StopRef {
  readonly stopId: string;
  readonly name: string;
  readonly latitude: number;
  readonly longitude: number;
}

export interface VehicleView {
  /** Vehicle number shown on the bus or train. */
  readonly vehicle: string;
  readonly mode: Mode | 'unknown';
  readonly route: ResolvedRouteRef | undefined;
  readonly headsign: string | undefined;
  readonly tripId: string | undefined;
  readonly latitude: number;
  readonly longitude: number;
  readonly bearingDegrees: number | undefined;
  readonly speedMph: number | undefined;
  readonly occupancy: string | undefined;
  readonly reportedAt: string | undefined;
  readonly positionAgeSeconds: number | undefined;
  readonly nextStop: NextStop | undefined;
}

export interface NextStop {
  readonly stopId: string;
  readonly name: string | undefined;
  readonly arrivalTime: string;
  readonly minutesAway: number;
}

export interface ArrivalView {
  readonly route: ResolvedRouteRef | undefined;
  readonly headsign: string | undefined;
  readonly vehicle: string | undefined;
  readonly tripId: string | undefined;
  readonly arrivalTime: string;
  readonly minutesAway: number;
  readonly scheduledArrivalTime: string | undefined;
  /** Positive means running late. Omitted when the feed gives no schedule. */
  readonly scheduleDeviationMinutes: number | undefined;
  readonly vehiclePosition: { readonly latitude: number; readonly longitude: number } | undefined;
}

const METERS_PER_SECOND_TO_MPH = 2.236_936_3;

function normalize(text: string): string {
  return text.trim().toLowerCase().replace(/\s+/g, ' ');
}

function toIsoTime(epochSeconds: number | undefined): string | undefined {
  if (epochSeconds === undefined) return undefined;
  return new Date(epochSeconds * 1000).toISOString();
}

function minutesBetween(epochSeconds: number, nowMs: number): number {
  return Math.round((epochSeconds * 1000 - nowMs) / 60_000);
}

/** Great-circle distance in meters. */
export function distanceMeters(
  fromLat: number,
  fromLon: number,
  toLat: number,
  toLon: number,
): number {
  const earthRadius = 6_371_000;
  const toRadians = (degrees: number): number => (degrees * Math.PI) / 180;
  const deltaLat = toRadians(toLat - fromLat);
  const deltaLon = toRadians(toLon - fromLon);
  const a =
    Math.sin(deltaLat / 2) ** 2 +
    Math.cos(toRadians(fromLat)) * Math.cos(toRadians(toLat)) * Math.sin(deltaLon / 2) ** 2;
  return 2 * earthRadius * Math.asin(Math.min(1, Math.sqrt(a)));
}

function toRouteRef(route: Route | undefined): ResolvedRouteRef | undefined {
  if (route === undefined) return undefined;
  return {
    routeId: route.routeId,
    name: route.shortName,
    longName: route.longName,
    mode: route.mode,
  };
}

/**
 * Resolves a user-supplied route query against the schedule.
 *
 * Matching is exact-first (`route_id`, then `route_short_name`) so a query of
 * "5" cannot be captured by "Route 501"; substring matching on the long name
 * is the last resort.
 */
export function findRoutes(schedule: Schedule, query: string): Route[] {
  const needle = normalize(query);
  if (needle === '') return [];
  const all = [...schedule.routes.values()];

  const byId = all.filter((route) => normalize(route.routeId) === needle);
  if (byId.length > 0) return byId;

  const byShortName = all.filter((route) => normalize(route.shortName) === needle);
  if (byShortName.length > 0) return byShortName;

  const byLongName = all.filter((route) => normalize(route.longName) === needle);
  if (byLongName.length > 0) return byLongName;

  return all.filter(
    (route) =>
      normalize(route.longName).includes(needle) || normalize(route.shortName).includes(needle),
  );
}

/**
 * Resolves a user-supplied stop query: exact `stop_id`/`stop_code` first, then
 * exact name, then a substring match ranked by name length so the closest
 * match to the query leads.
 */
export function findStops(schedule: Schedule, query: string, limit = 10): Stop[] {
  const needle = normalize(query);
  if (needle === '') return [];

  const direct = schedule.stops.get(query.trim());
  if (direct !== undefined) return [direct];

  const all = [...schedule.stops.values()];
  const byCode = all.filter((stop) => stop.code !== undefined && normalize(stop.code) === needle);
  if (byCode.length > 0) return byCode.slice(0, limit);

  const byName = all.filter((stop) => normalize(stop.name) === needle);
  if (byName.length > 0) return byName.slice(0, limit);

  return all
    .filter((stop) => normalize(stop.name).includes(needle))
    .sort((a, b) => a.name.length - b.name.length || a.name.localeCompare(b.name))
    .slice(0, limit);
}

/** Matches a vehicle query against its label, descriptor id, or entity id. */
export function matchesVehicleQuery(vehicle: VehiclePosition, query: string): boolean {
  const needle = normalize(query);
  if (needle === '') return false;
  const candidates = [vehicle.vehicleLabel, vehicle.vehicleId, vehicle.entityId];
  return candidates.some((value) => value !== undefined && normalize(value) === needle);
}

/**
 * Index of the soonest upcoming stop per trip, built from the TripUpdates feed.
 *
 * This is the only trustworthy source of a vehicle's next stop: the
 * VehiclePositions feed's own `stop_id` and `current_stop_sequence` do not
 * match the published schedule (see the note in `realtime.ts`).
 */
function buildNextStopIndex(
  tripUpdates: readonly TripUpdate[],
  nowMs: number,
): Map<string, { stopId: string; arrivalTime: number }> {
  const index = new Map<string, { stopId: string; arrivalTime: number }>();
  const nowSeconds = nowMs / 1000;

  for (const update of tripUpdates) {
    const tripId = update.tripId;
    if (tripId === undefined) continue;
    let best: { stopId: string; arrivalTime: number } | undefined;
    for (const stopTime of update.stopTimeUpdates) {
      const { stopId, arrivalTime } = stopTime;
      if (stopId === undefined || arrivalTime === undefined) continue;
      if (arrivalTime < nowSeconds) continue;
      if (best === undefined || arrivalTime < best.arrivalTime) {
        best = { stopId, arrivalTime };
      }
    }
    if (best !== undefined) index.set(tripId, best);
  }
  return index;
}

export interface TransitSnapshot {
  readonly schedule: Schedule;
  readonly vehicles: readonly VehiclePosition[];
  readonly tripUpdates: readonly TripUpdate[];
  readonly nowMs: number;
}

/** Builds the enriched, rider-facing view of a single vehicle. */
export function toVehicleView(
  vehicle: VehiclePosition,
  snapshot: TransitSnapshot,
  nextStopIndex: Map<string, { stopId: string; arrivalTime: number }>,
): VehicleView {
  const { schedule, nowMs } = snapshot;
  const trip = vehicle.tripId === undefined ? undefined : schedule.trips.get(vehicle.tripId);
  // Prefer the route the vehicle reports; fall back to the one on its trip.
  const routeId = vehicle.routeId ?? trip?.routeId;
  const route = routeId === undefined ? undefined : schedule.routes.get(routeId);

  const upcoming = vehicle.tripId === undefined ? undefined : nextStopIndex.get(vehicle.tripId);
  const nextStop: NextStop | undefined =
    upcoming === undefined
      ? undefined
      : {
          stopId: upcoming.stopId,
          name: schedule.stops.get(upcoming.stopId)?.name,
          arrivalTime: new Date(upcoming.arrivalTime * 1000).toISOString(),
          minutesAway: minutesBetween(upcoming.arrivalTime, nowMs),
        };

  return {
    vehicle: vehicle.vehicleLabel ?? vehicle.vehicleId ?? vehicle.entityId,
    mode: route?.mode ?? 'unknown',
    route: toRouteRef(route),
    headsign: trip?.headsign,
    tripId: vehicle.tripId,
    latitude: vehicle.latitude,
    longitude: vehicle.longitude,
    bearingDegrees: vehicle.bearingDegrees,
    speedMph:
      vehicle.speedMetersPerSecond === undefined
        ? undefined
        : Math.round(vehicle.speedMetersPerSecond * METERS_PER_SECOND_TO_MPH * 10) / 10,
    occupancy: vehicle.occupancyStatus,
    reportedAt: toIsoTime(vehicle.timestamp),
    positionAgeSeconds:
      vehicle.timestamp === undefined
        ? undefined
        : Math.max(0, Math.round(nowMs / 1000 - vehicle.timestamp)),
    nextStop,
  };
}

export function toVehicleViews(
  vehicles: readonly VehiclePosition[],
  snapshot: TransitSnapshot,
): VehicleView[] {
  const index = buildNextStopIndex(snapshot.tripUpdates, snapshot.nowMs);
  return vehicles.map((vehicle) => toVehicleView(vehicle, snapshot, index));
}

/** Predicted arrivals at one stop, soonest first. */
export function arrivalsAtStop(
  stop: Stop,
  snapshot: TransitSnapshot,
  options: { readonly routeIds?: ReadonlySet<string>; readonly mode?: Mode } = {},
): ArrivalView[] {
  const { schedule, tripUpdates, vehicles, nowMs } = snapshot;
  const nowSeconds = nowMs / 1000;

  const vehicleByTrip = new Map<string, VehiclePosition>();
  for (const vehicle of vehicles) {
    if (vehicle.tripId !== undefined) vehicleByTrip.set(vehicle.tripId, vehicle);
  }

  const arrivals: ArrivalView[] = [];
  for (const update of tripUpdates) {
    const trip = update.tripId === undefined ? undefined : schedule.trips.get(update.tripId);
    const routeId = update.routeId ?? trip?.routeId;
    const route = routeId === undefined ? undefined : schedule.routes.get(routeId);

    if (options.routeIds !== undefined && (routeId === undefined || !options.routeIds.has(routeId)))
      continue;
    if (options.mode !== undefined && route?.mode !== options.mode) continue;

    for (const stopTime of update.stopTimeUpdates) {
      if (stopTime.stopId !== stop.stopId) continue;
      if (stopTime.scheduleRelationship === 'SKIPPED') continue;
      const arrivalTime = stopTime.arrivalTime ?? stopTime.departureTime;
      if (arrivalTime === undefined || arrivalTime < nowSeconds) continue;

      const vehicle =
        update.tripId === undefined ? undefined : vehicleByTrip.get(update.tripId);
      const scheduled = stopTime.scheduledArrivalTime;

      arrivals.push({
        route: toRouteRef(route),
        headsign: trip?.headsign,
        vehicle: vehicle?.vehicleLabel ?? update.vehicleLabel,
        tripId: update.tripId,
        arrivalTime: new Date(arrivalTime * 1000).toISOString(),
        minutesAway: minutesBetween(arrivalTime, nowMs),
        scheduledArrivalTime: toIsoTime(scheduled),
        // The feed omits `delay`, so deviation is derived from the two stamps.
        scheduleDeviationMinutes:
          scheduled === undefined ? undefined : Math.round((arrivalTime - scheduled) / 60),
        vehiclePosition:
          vehicle === undefined
            ? undefined
            : { latitude: vehicle.latitude, longitude: vehicle.longitude },
      });
    }
  }

  return arrivals.sort((a, b) => a.arrivalTime.localeCompare(b.arrivalTime));
}

/** Alerts naming this stop or any of the given routes. */
export function alertsFor(
  alerts: readonly ServiceAlert[],
  stopId: string,
  routeIds: ReadonlySet<string>,
): ServiceAlert[] {
  return alerts.filter(
    (alert) =>
      alert.informedStopIds.includes(stopId) ||
      alert.informedRouteIds.some((routeId) => routeIds.has(routeId)),
  );
}

export { normalize };
