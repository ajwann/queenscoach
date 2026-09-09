import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { parseSchedule, type Schedule } from '../src/static-gtfs.ts';
import {
  decodeAlerts,
  decodeTripUpdates,
  decodeVehiclePositions,
  type ServiceAlert,
  type TripUpdate,
  type VehiclePosition,
} from '../src/realtime.ts';
import type { Cached } from '../src/cache.ts';
import type { Dependencies } from '../src/tools.ts';

const fixture = (name: string): Uint8Array =>
  new Uint8Array(readFileSync(fileURLToPath(new URL(`./fixtures/${name}`, import.meta.url))));

export const schedule: Schedule = parseSchedule(fixture('gtfs-static.zip'));
export const vehicles: VehiclePosition[] = decodeVehiclePositions(fixture('VehiclePositions.pb'));
export const tripUpdates: TripUpdate[] = decodeTripUpdates(fixture('TripUpdates.pb'));
export const alerts: ServiceAlert[] = decodeAlerts(fixture('Alerts.pb'));

/** Fixture capture time, so relative-time assertions are deterministic. */
export const CAPTURE_MS = 1_788_904_788_000;

const cached = <T>(value: T): Cached<T> => ({ value, fetchedAt: CAPTURE_MS });

export function fixtureDeps(overrides: Partial<Dependencies> = {}): Dependencies {
  return {
    loadSchedule: async () => cached(schedule),
    feeds: {
      vehiclePositions: async () => cached(vehicles),
      tripUpdates: async () => cached(tripUpdates),
      alerts: async () => cached(alerts),
    },
    now: () => CAPTURE_MS,
    ...overrides,
  };
}
