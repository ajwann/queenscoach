/**
 * Bearer-token authentication for the HTTP transport.
 *
 * The remote server is reachable by anyone who learns its URL, so it fails
 * closed: `createAuthenticator` refuses to build without a token unless the
 * operator explicitly opts into anonymous access.
 */

import { timingSafeEqual } from 'node:crypto';

/** Rejects tokens too short to resist guessing. */
const MIN_TOKEN_LENGTH = 24;

export class AuthConfigError extends Error {
  override readonly name = 'AuthConfigError';
}

export type AuthResult =
  | { readonly ok: true }
  | { readonly ok: false; readonly status: 401 | 403; readonly message: string };

/** Credentials a request can present. */
export interface PresentedCredentials {
  readonly authorizationHeader: string | undefined;
  /**
   * Token taken from the URL path (`/mcp/<token>`).
   *
   * Claude's custom-connector dialog accepts only a URL — it has no field for
   * a static header — so the path is the only way to give a hosted connector a
   * shared secret without implementing OAuth.
   */
  readonly pathToken: string | undefined;
}

/** Compares two secrets without leaking their contents through timing. */
function secretsMatch(provided: string, expected: string): boolean {
  const providedBytes = Buffer.from(provided, 'utf8');
  const expectedBytes = Buffer.from(expected, 'utf8');
  // timingSafeEqual throws on length mismatch, which would itself be a signal,
  // so compare against a same-length buffer and fold the length into the result.
  const sameLength = providedBytes.length === expectedBytes.length;
  const comparand = sameLength ? expectedBytes : providedBytes;
  const equal = timingSafeEqual(providedBytes, comparand);
  return sameLength && equal;
}

function extractBearer(header: string | undefined): string | undefined {
  if (header === undefined) return undefined;
  const match = /^Bearer[ \t]+(.+)$/i.exec(header.trim());
  return match?.[1]?.trim();
}

export interface AuthenticatorOptions {
  readonly token: string | undefined;
  readonly allowAnonymous: boolean;
}

/**
 * Builds the request authenticator.
 *
 * @throws {AuthConfigError} when no usable token is configured and anonymous
 * access was not explicitly enabled.
 */
export function createAuthenticator(
  options: AuthenticatorOptions,
): (presented: PresentedCredentials) => AuthResult {
  const { token, allowAnonymous } = options;

  if (token === undefined || token === '') {
    if (!allowAnonymous) {
      throw new AuthConfigError(
        'No CATS_AUTH_TOKEN is set. Set one to protect this public endpoint, ' +
          'or set CATS_ALLOW_ANONYMOUS=true to intentionally run it open.',
      );
    }
    return () => ({ ok: true });
  }

  if (token.length < MIN_TOKEN_LENGTH) {
    throw new AuthConfigError(
      `CATS_AUTH_TOKEN must be at least ${MIN_TOKEN_LENGTH} characters; ` +
        'generate one with: openssl rand -hex 32',
    );
  }

  return ({ authorizationHeader, pathToken }) => {
    const headerToken = extractBearer(authorizationHeader);
    const candidate = headerToken ?? pathToken;
    if (candidate === undefined) {
      return { ok: false, status: 401, message: 'Missing bearer token' };
    }
    if (!secretsMatch(candidate, token)) {
      return { ok: false, status: 403, message: 'Invalid bearer token' };
    }
    return { ok: true };
  };
}
