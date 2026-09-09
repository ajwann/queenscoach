import { test } from 'node:test';
import assert from 'node:assert/strict';
import { AuthConfigError, createAuthenticator } from '../src/auth.ts';

const TOKEN = 'a'.repeat(32);

/** Convenience: build the credentials object for a header-only request. */
const header = (authorizationHeader: string | undefined) => ({
  authorizationHeader,
  pathToken: undefined,
});
const path = (pathToken: string | undefined) => ({ authorizationHeader: undefined, pathToken });

test('refuses to start with no token unless anonymous access is explicit', () => {
  assert.throws(
    () => createAuthenticator({ token: undefined, allowAnonymous: false }),
    AuthConfigError,
  );
  assert.throws(() => createAuthenticator({ token: '', allowAnonymous: false }), AuthConfigError);
});

test('allows anonymous access only when explicitly opted in', () => {
  const authenticate = createAuthenticator({ token: undefined, allowAnonymous: true });
  assert.deepEqual(authenticate(header(undefined)), { ok: true });
});

test('rejects a token too short to resist guessing', () => {
  assert.throws(() => createAuthenticator({ token: 'short', allowAnonymous: false }), AuthConfigError);
});

test('accepts the correct bearer token', () => {
  const authenticate = createAuthenticator({ token: TOKEN, allowAnonymous: false });
  assert.deepEqual(authenticate(header(`Bearer ${TOKEN}`)), { ok: true });
});

test('is case-insensitive on the scheme and tolerates extra whitespace', () => {
  const authenticate = createAuthenticator({ token: TOKEN, allowAnonymous: false });
  assert.equal(authenticate(header(`bearer ${TOKEN}`)).ok, true);
  assert.equal(authenticate(header(`  BEARER   ${TOKEN}  `)).ok, true);
});

test('401 when the header is missing or not a bearer credential', () => {
  const authenticate = createAuthenticator({ token: TOKEN, allowAnonymous: false });
  assert.deepEqual(authenticate(header(undefined)), {
    ok: false,
    status: 401,
    message: 'Missing bearer token',
  });
  assert.equal(authenticate(header(`Basic ${TOKEN}`)).ok, false);
  assert.equal(authenticate(header('Bearer')).ok, false);
});

test('403 for a wrong token, including a prefix of the real one', () => {
  const authenticate = createAuthenticator({ token: TOKEN, allowAnonymous: false });
  const wrong = authenticate(header(`Bearer ${'b'.repeat(32)}`));
  assert.deepEqual(wrong, { ok: false, status: 403, message: 'Invalid bearer token' });
  // A length-mismatched guess must not be treated as a match.
  assert.equal(authenticate(header(`Bearer ${'a'.repeat(31)}`)).ok, false);
  assert.equal(authenticate(header(`Bearer ${'a'.repeat(33)}`)).ok, false);
});

test('a configured token is still required when anonymous access is allowed', () => {
  const authenticate = createAuthenticator({ token: TOKEN, allowAnonymous: true });
  assert.equal(authenticate(header(undefined)).ok, false);
  assert.equal(authenticate(header(`Bearer ${TOKEN}`)).ok, true);
});

test('accepts the token from the URL path, for the connector dialog', () => {
  const authenticate = createAuthenticator({ token: TOKEN, allowAnonymous: false });
  assert.deepEqual(authenticate(path(TOKEN)), { ok: true });
  assert.equal(authenticate(path('b'.repeat(32))).ok, false);
  assert.equal(authenticate(path(undefined)).ok, false);
});

test('a valid header wins even when the path segment is wrong', () => {
  const authenticate = createAuthenticator({ token: TOKEN, allowAnonymous: false });
  assert.equal(
    authenticate({ authorizationHeader: `Bearer ${TOKEN}`, pathToken: 'junk' }).ok,
    true,
  );
});
