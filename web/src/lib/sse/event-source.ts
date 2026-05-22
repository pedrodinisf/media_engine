/**
 * Thin EventSource wrapper that handles auth via query param.
 *
 * Native EventSource doesn't accept custom headers (XHR API gap), so we
 * append `?token=` to the URL. The server's `require_token` accepts it
 * alongside the bearer header.
 *
 * Callers pass an `onEvent` for typed-name dispatch (`event: <name>`
 * frame) and an `onError` for transport failures. The returned
 * function closes the connection.
 */

import { getTokenSync } from '$lib/stores/token';

export type SSEEvent = {
  /** Event name from the `event: ...` frame field. */
  type: string;
  /** Raw `data: ...` payload. Caller parses (it's always JSON in our protocol). */
  data: string;
};

export type SSEOptions = {
  onEvent: (event: SSEEvent) => void;
  onError?: (err: Event) => void;
  onOpen?: () => void;
  /** Event names to listen for in addition to "message". */
  events?: readonly string[];
};

const DEFAULT_EVENT_NAMES = [
  'OpStarted',
  'OpCompleted',
  'OpFailed',
  'Progress',
  'LogLine',
  'ArtifactReady',
] as const;

/** Open an SSE connection. Returns a `close()` callback. */
export function openSSE(path: string, options: SSEOptions): () => void {
  const token = getTokenSync();
  const sep = path.includes('?') ? '&' : '?';
  const url = token ? `${path}${sep}token=${encodeURIComponent(token)}` : path;
  const src = new EventSource(url);

  if (options.onOpen) {
    src.addEventListener('open', options.onOpen);
  }

  const names = options.events ?? DEFAULT_EVENT_NAMES;
  // Default unnamed `message` handler — useful when the server emits
  // anonymous data: frames (it doesn't today, but futureproof).
  src.onmessage = (ev: MessageEvent<string>) => {
    options.onEvent({ type: 'message', data: ev.data });
  };
  for (const name of names) {
    src.addEventListener(name, ((ev: MessageEvent<string>) => {
      options.onEvent({ type: name, data: ev.data });
    }) as EventListener);
  }

  if (options.onError) {
    src.addEventListener('error', options.onError);
  }

  return () => {
    src.close();
  };
}
