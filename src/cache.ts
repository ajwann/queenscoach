/** TTL cache with single-flight refresh, shared by the feed loaders. */

interface Entry<T> {
  readonly value: T;
  readonly fetchedAt: number;
}

export interface Cached<T> {
  readonly value: T;
  /** Epoch ms when the underlying data was fetched. */
  readonly fetchedAt: number;
}

/**
 * Wraps `load` so concurrent callers share one in-flight request and results
 * are reused until `ttlMs` elapses.
 *
 * The shared load deliberately takes no caller `AbortSignal`: one caller
 * cancelling must not abort a fetch other callers are awaiting. Loads bound
 * themselves with their own timeout instead.
 *
 * A failed load is not cached, but a stale entry is retained and returned so a
 * transient feed outage degrades to slightly old data rather than an error.
 */
export class TtlCache<T> {
  readonly #load: () => Promise<T>;
  readonly #ttlMs: number;
  #entry: Entry<T> | undefined;
  #inFlight: Promise<Entry<T>> | undefined;

  constructor(ttlMs: number, load: () => Promise<T>) {
    this.#ttlMs = ttlMs;
    this.#load = load;
  }

  async get(): Promise<Cached<T>> {
    const current = this.#entry;
    if (current !== undefined && Date.now() - current.fetchedAt < this.#ttlMs) {
      return current;
    }

    this.#inFlight ??= this.#refresh();
    try {
      return await this.#inFlight;
    } catch (error) {
      const stale = this.#entry;
      if (stale !== undefined) return stale;
      throw error;
    }
  }

  async #refresh(): Promise<Entry<T>> {
    try {
      const value = await this.#load();
      const entry: Entry<T> = { value, fetchedAt: Date.now() };
      this.#entry = entry;
      return entry;
    } finally {
      this.#inFlight = undefined;
    }
  }
}
