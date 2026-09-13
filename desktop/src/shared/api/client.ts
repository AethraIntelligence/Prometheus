/**
 * The only file in this application that knows how the runtime is reached.
 *
 * Everything above `shared` calls methods; nothing above it builds a URL, opens
 * an `EventSource` or knows the transport is HTTP at all. That is what makes
 * the React code an interface rather than a client library with a view attached
 * - and what would make moving to Tauri IPC, or to a remote engine later, a
 * change to one file on one layer.
 *
 * What it deliberately does not do matters as much. It never decides whether an
 * action is allowed, never chooses an employee, never retries work and never
 * interprets a result. Those belong to the core, and a convenience here that
 * guessed at any of them would be a second, disagreeing copy of a rule the
 * platform already enforces.
 */

import { DEFAULT_BASE_URL, SOURCE } from "./config";
import { transport, type Fetch } from "./transport";

export class RuntimeRequestError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "RuntimeRequestError";
  }
}

export class RuntimeClient {
  private readonly fetch: Fetch;

  constructor(
    private readonly baseUrl: string = DEFAULT_BASE_URL,
    fetchImpl?: Fetch,
  ) {
    // Resolved per client rather than per call: which `fetch` this is depends
    // on where the page is running, and that does not change while it runs.
    // A test hands one in; a window gets the shell's.
    this.fetch = fetchImpl ?? transport();
  }

  async get<T>(path: string): Promise<T> {
    return this.call<T>(path);
  }

  async post<T>(path: string, body?: unknown): Promise<T> {
    return this.call<T>(path, {
      method: "POST",
      body: JSON.stringify(body ?? {}),
    });
  }

  async put<T>(path: string, body?: unknown): Promise<T> {
    return this.call<T>(path, {
      method: "PUT",
      body: JSON.stringify(body ?? {}),
    });
  }

  async patch<T>(path: string, body?: unknown): Promise<T> {
    return this.call<T>(path, {
      method: "PATCH",
      body: JSON.stringify(body ?? {}),
    });
  }

  async del<T>(path: string): Promise<T> {
    return this.call<T>(path, { method: "DELETE" });
  }

  private async call<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await this.fetch(`${this.baseUrl}${path}`, {
      headers: { "Content-Type": "application/json" },
      ...init,
    });
    if (!response.ok) {
      // The runtime explains itself in `detail`, in words written for a person
      // - "Unknown employee: x", "run the migration". Showing that beats a
      // status code, and inventing a friendlier sentence here would hide the
      // one thing that tells the user what to do next.
      const detail = await response
        .json()
        .then((body) => body?.detail)
        .catch(() => null);
      throw new RuntimeRequestError(detail ?? response.statusText, response.status);
    }
    return (await response.json()) as T;
  }

  /**
   * Subscribe to a server-sent stream. Returns the unsubscribe.
   *
   * Read off the response body rather than through `EventSource`. Two reasons,
   * and the first one is decisive: `EventSource` always uses the webview's own
   * network stack, which is exactly what a packaged window cannot use. The
   * second is that one transport for requests and streams means one place where
   * "how the runtime is reached" is answered.
   *
   * A line that cannot be parsed is skipped: a trace is not the record of what
   * happened - that is in the store - so losing one must never take the window
   * down with it.
   */
  stream<T>(path: string, onEvent: (event: T) => void): () => void {
    const controller = new AbortController();
    void (async () => {
      try {
        const response = await this.fetch(`${this.baseUrl}${path}`, {
          headers: { Accept: "text/event-stream" },
          signal: controller.signal,
        });
        const body = response.body;
        if (!body) return;
        const reader = body.getReader();
        const decoder = new TextDecoder();
        let buffered = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) return;
          buffered += decoder.decode(value, { stream: true });
          // Server-sent events are separated by a blank line; a keep-alive is
          // a comment line and parses to nothing, which is what it means.
          const frames = buffered.split("\n\n");
          buffered = frames.pop() ?? "";
          for (const frame of frames) {
            for (const line of frame.split("\n")) {
              if (!line.startsWith("data: ")) continue;
              try {
                onEvent(JSON.parse(line.slice(6)) as T);
              } catch {
                /* skipped on purpose */
              }
            }
          }
        }
      } catch {
        // A stream that ends, or a window that closed it, is not an error
        // worth showing: what happened is read back from the store.
      }
    })();
    return () => controller.abort();
  }
}

export { DEFAULT_BASE_URL, SOURCE };
