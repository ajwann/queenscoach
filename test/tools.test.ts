import { test } from 'node:test';
import assert from 'node:assert/strict';
import { findVehicle, getArrivals, listVehicles } from '../src/tools.ts';
import { fixtureDeps, schedule, vehicles } from './helpers.ts';

const deps = fixtureDeps();

test('static schedule classifies light rail as train and buses as bus', () => {
  assert.equal(schedule.routes.get('501')?.mode, 'train');
  assert.equal(schedule.routes.get('510')?.mode, 'train');
  assert.equal(schedule.routes.get('29')?.mode, 'bus');
  assert.equal(schedule.routes.get('501')?.longName, 'Light Rail - Lynx Blue Line');
});

test('find_vehicle locates one vehicle and returns coordinates in the Charlotte area', async () => {
  const result = await findVehicle(deps, { vehicle: '2301' });
  const found = result['vehicles'] as Array<Record<string, unknown>>;
  assert.equal(result['matches'], 1);
  const vehicle = found[0];
  assert.equal(vehicle?.['vehicle'], '2301');
  assert.ok((vehicle?.['latitude'] as number) > 34.9 && (vehicle?.['latitude'] as number) < 35.7);
  assert.ok((vehicle?.['longitude'] as number) > -81.2 && (vehicle?.['longitude'] as number) < -80.4);
  assert.equal((vehicle?.['route'] as Record<string, unknown>)?.['name'], '29');
  assert.equal(vehicle?.['mode'], 'bus');
});

test('find_vehicle by route returns only that route and reports mode', async () => {
  const result = await findVehicle(deps, { route: '501' });
  const found = result['vehicles'] as Array<Record<string, unknown>>;
  assert.ok(found.length > 0, 'expected Blue Line trains in the fixture');
  for (const vehicle of found) {
    assert.equal((vehicle['route'] as Record<string, unknown>)['name'], '501');
    assert.equal(vehicle['mode'], 'train');
  }
});

test('find_vehicle route query "5" does not match route 501 or 510', async () => {
  const result = await findVehicle(deps, { route: '5' });
  assert.deepEqual(result['matchedRoutes'], ['5']);
});

test('find_vehicle requires an argument', async () => {
  const result = await findVehicle(deps, {});
  assert.match(String(result['error']), /Provide "vehicle" or "route"|Provide "vehicle"/);
});

test('find_vehicle reports a clear miss for an out-of-service vehicle', async () => {
  const result = await findVehicle(deps, { vehicle: '000-not-real' });
  assert.equal(result['matches'], 0);
  assert.match(String(result['message']), /not reporting a position/);
});

test('find_vehicle honors the mode filter', async () => {
  const result = await findVehicle(deps, { vehicle: '2301', mode: 'train' });
  assert.equal(result['matches'], 0);
});

test('list_vehicles returns every reporting vehicle with coordinates', async () => {
  const result = await listVehicles(deps, {});
  assert.equal(result['totalInService'], vehicles.length);
  const listed = result['vehicles'] as Array<Record<string, unknown>>;
  for (const vehicle of listed) {
    assert.equal(typeof vehicle['latitude'], 'number');
    assert.equal(typeof vehicle['longitude'], 'number');
  }
  const counts = result['countsByMode'] as Record<string, number>;
  assert.ok(counts['bus']! > 0 && counts['train']! > 0);
});

test('list_vehicles applies mode filter and limit', async () => {
  const trains = await listVehicles(deps, { mode: 'train' });
  for (const vehicle of trains['vehicles'] as Array<Record<string, unknown>>) {
    assert.equal(vehicle['mode'], 'train');
  }

  const limited = await listVehicles(deps, { limit: 3 });
  assert.equal((limited['vehicles'] as unknown[]).length, 3);
  assert.equal(limited['returned'], 3);
  assert.equal(limited['totalInService'], vehicles.length);
});

test('get_arrivals resolves a stop id and returns sorted future arrivals', async () => {
  const result = await getArrivals(deps, { stop: '00285' });
  const stop = result['stop'] as Record<string, unknown>;
  assert.equal(stop['stopId'], '00285');
  assert.equal(stop['name'], 'Albemarle Rd & Lawyers Rd Park and Ride');

  const arrivals = result['arrivals'] as Array<Record<string, unknown>>;
  assert.ok(arrivals.length > 0, 'expected predicted arrivals at this stop');

  let previous = '';
  for (const arrival of arrivals) {
    const time = String(arrival['arrivalTime']);
    assert.ok(time >= previous, 'arrivals must be sorted soonest first');
    previous = time;
    assert.ok((arrival['minutesAway'] as number) >= 0, 'past arrivals must be filtered out');
    assert.equal(typeof arrival['scheduleDeviationMinutes'], 'number');
  }
});

test('get_arrivals resolves a stop by name substring and lists other candidates', async () => {
  const result = await getArrivals(deps, { stop: 'Central Ave' });
  const stop = result['stop'] as Record<string, unknown>;
  assert.match(String(stop['name']), /Central Ave/);
  const others = result['otherStopsMatchingQuery'] as Array<Record<string, unknown>>;
  assert.ok(others.length > 0, 'ambiguous name should surface the other matches');
  for (const other of others) assert.equal(typeof other['metersAway'], 'number');
});

test('get_arrivals excludes predictions that are already in the past', async () => {
  // Every prediction for this stop in the fixture snapshot precedes the
  // capture time, so the tool must report none rather than negative ETAs.
  const result = await getArrivals(deps, { stop: '02400' });
  assert.equal((result['stop'] as Record<string, unknown>)['stopId'], '02400');
  assert.equal(result['arrivalCount'], 0);
  assert.match(String(result['message']), /No realtime arrivals/);
});

test('get_arrivals filters by route and by mode', async () => {
  const byRoute = await getArrivals(deps, { stop: '00285', route: '29' });
  for (const arrival of byRoute['arrivals'] as Array<Record<string, unknown>>) {
    assert.equal((arrival['route'] as Record<string, unknown>)['name'], '29');
  }

  const trains = await getArrivals(deps, { stop: '00285', mode: 'train' });
  assert.equal(trains['arrivalCount'], 0, 'no light rail serves this bus stop');
});

test('get_arrivals reports an unmatched stop instead of guessing', async () => {
  const result = await getArrivals(deps, { stop: 'zzzz no such stop' });
  assert.match(String(result['error']), /No stop matched/);
});

test('get_arrivals limit caps the list but arrivalCount reports the total', async () => {
  const all = await getArrivals(deps, { stop: '00285', limit: 50 });
  const capped = await getArrivals(deps, { stop: '00285', limit: 1 });
  assert.equal(capped['arrivalCount'], all['arrivalCount']);
  assert.equal((capped['arrivals'] as unknown[]).length, 1);
});

test('get_arrivals still answers when the alerts feed fails', async () => {
  const failing = fixtureDeps();
  const result = await getArrivals(
    {
      ...failing,
      feeds: {
        ...failing.feeds,
        alerts: async () => {
          throw new Error('alerts feed down');
        },
      },
    },
    { stop: '00285' },
  );
  assert.equal(result['serviceAlerts'], undefined);
  assert.ok((result['arrivals'] as unknown[]).length > 0);
});
