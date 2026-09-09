import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ConfigError, loadConfig } from '../src/config.ts';

test('defaults point at the published CATS feeds', () => {
  const config = loadConfig({});
  assert.match(config.vehiclePositionsUrl, /VehiclePositions\.pb$/);
  assert.match(config.tripUpdatesUrl, /TripUpdates\.pb$/);
  assert.match(config.alertsUrl, /Alerts\.pb$/);
  assert.ok(config.realtimeTtlMs > 0);
});

test('accepts an http(s) override', () => {
  const config = loadConfig({ CATS_ALERTS_URL: 'https://example.test/a.pb' });
  assert.equal(config.alertsUrl, 'https://example.test/a.pb');
});

test('rejects a non-http scheme', () => {
  assert.throws(
    () => loadConfig({ CATS_ALERTS_URL: 'file:///etc/passwd' }),
    (error: unknown) => error instanceof ConfigError && /http or https/.test(error.message),
  );
});

test('rejects a malformed URL and a non-positive number', () => {
  assert.throws(() => loadConfig({ CATS_ALERTS_URL: 'not a url' }), ConfigError);
  assert.throws(() => loadConfig({ CATS_REALTIME_TTL_MS: '0' }), ConfigError);
  assert.throws(() => loadConfig({ CATS_REALTIME_TTL_MS: 'soon' }), ConfigError);
});

test('an empty environment variable falls back to the default', () => {
  const config = loadConfig({ CATS_ALERTS_URL: '', CATS_REALTIME_TTL_MS: '' });
  assert.match(config.alertsUrl, /Alerts\.pb$/);
  assert.ok(config.realtimeTtlMs > 0);
});
