/** Bounded HTTP fetch helpers shared by the static and realtime loaders. */

export class FeedFetchError extends Error {
  override readonly name = 'FeedFetchError';
  readonly url: string;
  readonly status: number | undefined;

  constructor(message: string, url: string, options: { status?: number; cause?: unknown } = {}) {
    super(message, options.cause === undefined ? undefined : { cause: options.cause });
    this.url = url;
    this.status = options.status;
  }
}

export interface FetchOptions {
  readonly timeoutMs: number;
  readonly maxBytes: number;
  readonly signal?: AbortSignal | undefined;
}

/**
 * Fetches a URL into memory with an explicit timeout and a hard byte ceiling.
 *
 * The body is read incrementally so an oversized or endless response is
 * abandoned as soon as it crosses `maxBytes` rather than after buffering it.
 */
export async function fetchBinary(url: string, options: FetchOptions): Promise<Uint8Array> {
  const timeout = AbortSignal.timeout(options.timeoutMs);
  const signal =
    options.signal === undefined ? timeout : AbortSignal.any([timeout, options.signal]);

  let response: Response;
  try {
    response = await fetch(url, {
      signal,
      redirect: 'follow',
      headers: { accept: 'application/octet-stream, application/x-zip-compressed, */*' },
    });
  } catch (cause) {
    throw new FeedFetchError(`Request to ${url} failed: ${describe(cause)}`, url, { cause });
  }

  if (!response.ok) {
    throw new FeedFetchError(
      `Request to ${url} returned HTTP ${response.status} ${response.statusText}`.trim(),
      url,
      { status: response.status },
    );
  }

  const declared = Number(response.headers.get('content-length'));
  if (Number.isFinite(declared) && declared > options.maxBytes) {
    throw new FeedFetchError(
      `Response from ${url} declares ${declared} bytes, over the ${options.maxBytes} byte limit`,
      url,
    );
  }

  const body = response.body;
  if (body === null) {
    throw new FeedFetchError(`Response from ${url} had no body`, url);
  }

  const chunks: Uint8Array[] = [];
  let total = 0;
  const reader = body.getReader();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value === undefined) continue;
      total += value.byteLength;
      if (total > options.maxBytes) {
        throw new FeedFetchError(
          `Response from ${url} exceeded the ${options.maxBytes} byte limit`,
          url,
        );
      }
      chunks.push(value);
    }
  } catch (cause) {
    if (cause instanceof FeedFetchError) throw cause;
    throw new FeedFetchError(`Reading ${url} failed: ${describe(cause)}`, url, { cause });
  } finally {
    // Releases the socket when we bail out early; a no-op on normal completion.
    await reader.cancel().catch(() => undefined);
  }

  const merged = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    merged.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return merged;
}

function describe(cause: unknown): string {
  if (cause instanceof Error) {
    // Message alone; stack traces and internal paths stay out of tool output.
    return cause.name === 'TimeoutError' ? 'timed out' : cause.message;
  }
  return 'unknown error';
}
