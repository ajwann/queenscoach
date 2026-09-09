import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseCsv } from '../src/csv.ts';

test('parses quoted fields containing commas', () => {
  const rows = parseCsv('route_id,route_long_name\n1,"Mt. Holly Road, North"\n');
  assert.equal(rows.length, 1);
  assert.equal(rows[0]?.['route_long_name'], 'Mt. Holly Road, North');
});

test('handles escaped quotes, CRLF, and a BOM', () => {
  const rows = parseCsv('﻿a,b\r\n"say ""hi""",2\r\n');
  assert.equal(rows[0]?.['a'], 'say "hi"');
  assert.equal(rows[0]?.['b'], '2');
});

test('empty fields become undefined and short rows do not throw', () => {
  const rows = parseCsv('a,b,c\n1,,\n2\n');
  assert.equal(rows[0]?.['b'], undefined);
  assert.equal(rows[1]?.['a'], '2');
  assert.equal(rows[1]?.['c'], undefined);
});

test('header-only input yields no rows', () => {
  assert.deepEqual(parseCsv('a,b\n'), []);
});
