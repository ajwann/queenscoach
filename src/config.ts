/**
 * Runtime configuration. Every value has a working default so the server
 * starts with no environment set; overrides are validated at startup.
 */

export interface Config {
  readonly vehiclePositionsUrl: string;
  readonly tripUpdatesUrl: string;
  readonly alertsUrl: string;
  readonly staticGtfsUrl: string;
  /** How long a decoded realtime feed is reused before refetching. */
  readonly realtimeTtlMs: number;
  /** How long the parsed static schedule is reused before refetching. */
  readonly staticTtlMs: number;
  readonly requestTimeoutMs: number;
  /** Guards against a mis-pointed URL streaming an unbounded body at us. */
  readonly maxFeedBytes: number;
  readonly maxStaticBytes: number;
  /** HTTP transport: listen port. */
  readonly httpPort: number;
  /** HTTP transport: bind address. Containers need 0.0.0.0, not localhost. */
  readonly httpHost: string;
  /** HTTP transport: largest JSON-RPC request body accepted. */
  readonly maxBodyBytes: number;
}

const DEFAULTS: Config = {
  vehiclePositionsUrl:
    'https://gtfsrealtime.ridetransit.org/GTFSRealTime/Vehicle/VehiclePositions.pb',
  tripUpdatesUrl:
    'https://gtfsrealtime.ridetransit.org/GTFSRealTime/TripUpdate/TripUpdates.pb',
  alertsUrl: 'https://gtfsrealtime.ridetransit.org/GTFSRealTime/Alert/Alerts.pb',
  staticGtfsUrl: 'https://gtfsrealtime.ridetransit.org/GTFSStatic/api/GTFSDownload/GTFS.zip',
  realtimeTtlMs: 20_000,
  staticTtlMs: 6 * 60 * 60 * 1000,
  requestTimeoutMs: 30_000,
  maxFeedBytes: 32 * 1024 * 1024,
  maxStaticBytes: 256 * 1024 * 1024,
  httpPort: 8080,
  httpHost: '0.0.0.0',
  maxBodyBytes: 4 * 1024 * 1024,
};

class ConfigError extends Error {
  override readonly name = 'ConfigError';
}

function readUrl(env: NodeJS.ProcessEnv, key: string, fallback: string): string {
  const raw = env[key];
  if (raw === undefined || raw === '') return fallback;
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    throw new ConfigError(`${key} is not a valid URL`);
  }
  // Feeds are fetched by the server itself; refuse anything but plain HTTP(S).
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') {
    throw new ConfigError(`${key} must use http or https, got ${parsed.protocol}`);
  }
  return parsed.toString();
}

function readPositiveInt(env: NodeJS.ProcessEnv, key: string, fallback: number): number {
  const raw = env[key];
  if (raw === undefined || raw === '') return fallback;
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new ConfigError(`${key} must be a positive integer, got ${JSON.stringify(raw)}`);
  }
  return value;
}

function readPort(env: NodeJS.ProcessEnv, fallback: number): number {
  const explicit = env['CATS_HTTP_PORT'];
  const port = readPositiveInt(
    env,
    explicit === undefined || explicit === '' ? 'PORT' : 'CATS_HTTP_PORT',
    fallback,
  );
  if (port > 65_535) {
    throw new ConfigError(`port must be between 1 and 65535, got ${port}`);
  }
  return port;
}

/** Builds config from the environment, throwing `ConfigError` on bad input. */
export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  return {
    vehiclePositionsUrl: readUrl(env, 'CATS_VEHICLE_POSITIONS_URL', DEFAULTS.vehiclePositionsUrl),
    tripUpdatesUrl: readUrl(env, 'CATS_TRIP_UPDATES_URL', DEFAULTS.tripUpdatesUrl),
    alertsUrl: readUrl(env, 'CATS_ALERTS_URL', DEFAULTS.alertsUrl),
    staticGtfsUrl: readUrl(env, 'CATS_STATIC_GTFS_URL', DEFAULTS.staticGtfsUrl),
    realtimeTtlMs: readPositiveInt(env, 'CATS_REALTIME_TTL_MS', DEFAULTS.realtimeTtlMs),
    staticTtlMs: readPositiveInt(env, 'CATS_STATIC_TTL_MS', DEFAULTS.staticTtlMs),
    requestTimeoutMs: readPositiveInt(env, 'CATS_REQUEST_TIMEOUT_MS', DEFAULTS.requestTimeoutMs),
    maxFeedBytes: readPositiveInt(env, 'CATS_MAX_FEED_BYTES', DEFAULTS.maxFeedBytes),
    maxStaticBytes: readPositiveInt(env, 'CATS_MAX_STATIC_BYTES', DEFAULTS.maxStaticBytes),
    // PORT is what most container platforms inject; CATS_HTTP_PORT overrides it.
    httpPort: readPort(env, DEFAULTS.httpPort),
    httpHost: env['CATS_HTTP_HOST'] ?? DEFAULTS.httpHost,
    maxBodyBytes: readPositiveInt(env, 'CATS_MAX_BODY_BYTES', DEFAULTS.maxBodyBytes),
  };
}

export { ConfigError };
