/**
 * GTFS-Realtime feed access: vehicle positions, trip updates, service alerts.
 *
 * Feed quirks observed in the live CATS data, which the shapes below reflect:
 *
 * - `VehiclePosition.stop_id` and `current_stop_sequence` do not correspond to
 *   any stop or sequence in the published static schedule (0% of stop ids
 *   resolved, and sequences exceed the trip's own stop count). They are
 *   therefore never surfaced as stop references; next-stop information comes
 *   from the TripUpdates feed, whose stop ids resolve completely.
 * - `StopTimeEvent.delay` is never populated, but `time` and `scheduled_time`
 *   both are, so schedule deviation is computed from those.
 */

import GtfsRealtimeBindings from 'gtfs-realtime-bindings';
import type { Config } from './config.ts';
import { fetchBinary } from './http.ts';
import { TtlCache, type Cached } from './cache.ts';

const { FeedMessage } = GtfsRealtimeBindings.transit_realtime;

export interface VehiclePosition {
  /** Agency vehicle number as shown on the bus or train, e.g. `2301`. */
  readonly vehicleLabel: string | undefined;
  readonly vehicleId: string | undefined;
  readonly entityId: string;
  readonly tripId: string | undefined;
  readonly routeId: string | undefined;
  readonly latitude: number;
  readonly longitude: number;
  readonly bearingDegrees: number | undefined;
  readonly speedMetersPerSecond: number | undefined;
  readonly occupancyStatus: string | undefined;
  readonly currentStatus: string | undefined;
  /** Epoch seconds the position was reported. */
  readonly timestamp: number | undefined;
}

export interface StopTimeUpdate {
  readonly stopId: string | undefined;
  readonly stopSequence: number | undefined;
  /** Predicted arrival, epoch seconds. */
  readonly arrivalTime: number | undefined;
  readonly scheduledArrivalTime: number | undefined;
  readonly departureTime: number | undefined;
  readonly scheduleRelationship: string | undefined;
}

export interface TripUpdate {
  readonly tripId: string | undefined;
  readonly routeId: string | undefined;
  readonly vehicleLabel: string | undefined;
  readonly vehicleId: string | undefined;
  readonly stopTimeUpdates: readonly StopTimeUpdate[];
}

export interface ServiceAlert {
  readonly headerText: string | undefined;
  readonly descriptionText: string | undefined;
  readonly cause: string | undefined;
  readonly effect: string | undefined;
  readonly severityLevel: string | undefined;
  readonly informedRouteIds: readonly string[];
  readonly informedStopIds: readonly string[];
}

/** Protobuf 64-bit fields decode as `string` under `longs: String`. */
function toEpochSeconds(value: unknown): number | undefined {
  if (value === undefined || value === null) return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function toFiniteNumber(value: unknown): number | undefined {
  if (typeof value !== 'number' || !Number.isFinite(value)) return undefined;
  return value;
}

function toText(translated: unknown): string | undefined {
  if (typeof translated !== 'object' || translated === null) return undefined;
  const translations = (translated as { translation?: unknown }).translation;
  if (!Array.isArray(translations)) return undefined;
  const english = translations.find(
    (entry) => typeof entry?.language === 'string' && entry.language.startsWith('en'),
  );
  const chosen = english ?? translations[0];
  const text = (chosen as { text?: unknown } | undefined)?.text;
  return typeof text === 'string' && text.length > 0 ? text : undefined;
}

/**
 * Decodes a GTFS-Realtime protobuf into plain objects.
 *
 * Feed payloads are untrusted input: every field is re-checked here rather
 * than assumed present, because the protobuf schema makes almost everything
 * optional and this agency omits several fields entirely.
 */
function decodeFeed(bytes: Uint8Array): { header: unknown; entities: readonly unknown[] } {
  const message = FeedMessage.decode(bytes);
  const object = FeedMessage.toObject(message, {
    longs: String,
    enums: String,
    defaults: false,
  }) as { header?: unknown; entity?: unknown };
  const entities = Array.isArray(object.entity) ? object.entity : [];
  return { header: object.header, entities };
}

export function decodeVehiclePositions(bytes: Uint8Array): VehiclePosition[] {
  const { entities } = decodeFeed(bytes);
  const vehicles: VehiclePosition[] = [];

  for (const entity of entities) {
    const record = entity as { id?: unknown; vehicle?: Record<string, unknown> };
    const vehicle = record.vehicle;
    if (vehicle === undefined) continue;

    const position = vehicle['position'] as Record<string, unknown> | undefined;
    const latitude = toFiniteNumber(position?.['latitude']);
    const longitude = toFiniteNumber(position?.['longitude']);
    // Without coordinates the entry cannot answer a location question.
    if (latitude === undefined || longitude === undefined) continue;

    const trip = vehicle['trip'] as Record<string, unknown> | undefined;
    const descriptor = vehicle['vehicle'] as Record<string, unknown> | undefined;

    vehicles.push({
      entityId: typeof record.id === 'string' ? record.id : '',
      vehicleLabel: typeof descriptor?.['label'] === 'string' ? descriptor['label'] : undefined,
      vehicleId: typeof descriptor?.['id'] === 'string' ? descriptor['id'] : undefined,
      tripId: typeof trip?.['tripId'] === 'string' ? trip['tripId'] : undefined,
      routeId: typeof trip?.['routeId'] === 'string' ? trip['routeId'] : undefined,
      latitude,
      longitude,
      bearingDegrees: toFiniteNumber(position?.['bearing']),
      speedMetersPerSecond: toFiniteNumber(position?.['speed']),
      occupancyStatus:
        typeof vehicle['occupancyStatus'] === 'string' ? vehicle['occupancyStatus'] : undefined,
      currentStatus:
        typeof vehicle['currentStatus'] === 'string' ? vehicle['currentStatus'] : undefined,
      timestamp: toEpochSeconds(vehicle['timestamp']),
    });
  }
  return vehicles;
}

export function decodeTripUpdates(bytes: Uint8Array): TripUpdate[] {
  const { entities } = decodeFeed(bytes);
  const updates: TripUpdate[] = [];

  for (const entity of entities) {
    const update = (entity as { tripUpdate?: Record<string, unknown> }).tripUpdate;
    if (update === undefined) continue;

    const trip = update['trip'] as Record<string, unknown> | undefined;
    const descriptor = update['vehicle'] as Record<string, unknown> | undefined;
    const rawStopTimes = update['stopTimeUpdate'];
    const stopTimeUpdates: StopTimeUpdate[] = [];

    if (Array.isArray(rawStopTimes)) {
      for (const raw of rawStopTimes) {
        const stopTime = raw as Record<string, unknown>;
        const arrival = stopTime['arrival'] as Record<string, unknown> | undefined;
        const departure = stopTime['departure'] as Record<string, unknown> | undefined;
        stopTimeUpdates.push({
          stopId: typeof stopTime['stopId'] === 'string' ? stopTime['stopId'] : undefined,
          stopSequence: toFiniteNumber(stopTime['stopSequence']),
          arrivalTime: toEpochSeconds(arrival?.['time']),
          scheduledArrivalTime: toEpochSeconds(arrival?.['scheduledTime']),
          departureTime: toEpochSeconds(departure?.['time']),
          scheduleRelationship:
            typeof stopTime['scheduleRelationship'] === 'string'
              ? stopTime['scheduleRelationship']
              : undefined,
        });
      }
    }

    updates.push({
      tripId: typeof trip?.['tripId'] === 'string' ? trip['tripId'] : undefined,
      routeId: typeof trip?.['routeId'] === 'string' ? trip['routeId'] : undefined,
      vehicleLabel: typeof descriptor?.['label'] === 'string' ? descriptor['label'] : undefined,
      vehicleId: typeof descriptor?.['id'] === 'string' ? descriptor['id'] : undefined,
      stopTimeUpdates,
    });
  }
  return updates;
}

export function decodeAlerts(bytes: Uint8Array): ServiceAlert[] {
  const { entities } = decodeFeed(bytes);
  const alerts: ServiceAlert[] = [];

  for (const entity of entities) {
    const alert = (entity as { alert?: Record<string, unknown> }).alert;
    if (alert === undefined) continue;

    const routeIds = new Set<string>();
    const stopIds = new Set<string>();
    const informed = alert['informedEntity'];
    if (Array.isArray(informed)) {
      for (const raw of informed) {
        const selector = raw as Record<string, unknown>;
        if (typeof selector['routeId'] === 'string') routeIds.add(selector['routeId']);
        if (typeof selector['stopId'] === 'string') stopIds.add(selector['stopId']);
      }
    }

    alerts.push({
      headerText: toText(alert['headerText']),
      descriptionText: toText(alert['descriptionText']),
      cause: typeof alert['cause'] === 'string' ? alert['cause'] : undefined,
      effect: typeof alert['effect'] === 'string' ? alert['effect'] : undefined,
      severityLevel:
        typeof alert['severityLevel'] === 'string' ? alert['severityLevel'] : undefined,
      informedRouteIds: [...routeIds],
      informedStopIds: [...stopIds],
    });
  }
  return alerts;
}

export interface RealtimeFeeds {
  vehiclePositions(): Promise<Cached<VehiclePosition[]>>;
  tripUpdates(): Promise<Cached<TripUpdate[]>>;
  alerts(): Promise<Cached<ServiceAlert[]>>;
}

export function createRealtimeFeeds(config: Config): RealtimeFeeds {
  const build = <T>(url: string, decode: (bytes: Uint8Array) => T): TtlCache<T> =>
    new TtlCache<T>(config.realtimeTtlMs, async () => {
      const bytes = await fetchBinary(url, {
        timeoutMs: config.requestTimeoutMs,
        maxBytes: config.maxFeedBytes,
      });
      return decode(bytes);
    });

  const vehicles = build(config.vehiclePositionsUrl, decodeVehiclePositions);
  const trips = build(config.tripUpdatesUrl, decodeTripUpdates);
  const alerts = build(config.alertsUrl, decodeAlerts);

  return {
    vehiclePositions: () => vehicles.get(),
    tripUpdates: () => trips.get(),
    alerts: () => alerts.get(),
  };
}
