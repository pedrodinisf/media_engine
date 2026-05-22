/**
 * Thin REST client.
 *
 * Adds the bearer header (from the token store), normalizes errors into
 * a single `ApiError` shape so components don't have to special-case 4xx
 * vs network failures, and serializes JSON bodies. SSE lives in `lib/sse`
 * because it has its own auth flow (query param).
 */

import { getTokenSync } from '$lib/stores/token';

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
    public readonly raw?: unknown,
  ) {
    super(`API ${status}: ${detail}`);
    this.name = 'ApiError';
  }
}

type FetchInit = Omit<RequestInit, 'body' | 'headers'> & {
  body?: BodyInit | null | object;
  headers?: Record<string, string>;
};

async function request<T>(path: string, init: FetchInit = {}): Promise<T> {
  const token = getTokenSync();
  const headers: Record<string, string> = {
    ...(init.headers ?? {}),
  };
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  let body: BodyInit | null | undefined = undefined;
  if (init.body !== undefined && init.body !== null) {
    if (
      init.body instanceof FormData ||
      init.body instanceof Blob ||
      init.body instanceof ArrayBuffer ||
      typeof init.body === 'string'
    ) {
      body = init.body;
    } else {
      headers['Content-Type'] = headers['Content-Type'] ?? 'application/json';
      body = JSON.stringify(init.body);
    }
  }

  // Strip our own `body` field before forwarding to fetch (it accepts a
  // different shape than FetchInit.body) and only add the resolved one
  // when defined — exactOptionalPropertyTypes forbids body: undefined.
  const { body: _ignored, ...rest } = init;
  const fetchInit: RequestInit =
    body !== undefined ? { ...rest, headers, body } : { ...rest, headers };
  const res = await fetch(path, fetchInit);

  // 204 / 205 carry no body — let the call site default an empty value.
  if (res.status === 204 || res.status === 205) {
    return undefined as T;
  }

  const contentType = res.headers.get('content-type') ?? '';
  const payload = contentType.includes('application/json')
    ? await res.json()
    : await res.text();

  if (!res.ok) {
    const detail =
      (typeof payload === 'object' && payload !== null && 'detail' in payload
        ? String((payload as { detail: unknown }).detail)
        : null) ??
      (typeof payload === 'string' ? payload : res.statusText);
    throw new ApiError(res.status, detail, payload);
  }

  return payload as T;
}

function withBody(init: FetchInit, body: BodyInit | object | undefined): FetchInit {
  // exactOptionalPropertyTypes: setting body to undefined is a type error,
  // so we only include the key when it's actually present.
  return body === undefined ? init : { ...init, body };
}

export const api = {
  get: <T>(path: string, init: FetchInit = {}) =>
    request<T>(path, { ...init, method: 'GET' }),
  post: <T>(path: string, body?: object, init: FetchInit = {}) =>
    request<T>(path, withBody({ ...init, method: 'POST' }, body)),
  postForm: <T>(path: string, form: FormData, init: FetchInit = {}) =>
    request<T>(path, { ...init, method: 'POST', body: form }),
  put: <T>(path: string, body?: object, init: FetchInit = {}) =>
    request<T>(path, withBody({ ...init, method: 'PUT' }, body)),
  delete: <T>(path: string, init: FetchInit = {}) =>
    request<T>(path, { ...init, method: 'DELETE' }),
};

/** Shared response shape for /run + /pipelines submissions. */
export type JobAck = { job_id: string };

/** Upload preview from `POST /acquire/upload?commit=false`. */
export type UploadPreview = {
  kind: string;
  duration_s: number | null;
  codec: string | null;
  width: number | null;
  height: number | null;
  size_bytes: number;
  sha256_prefix: string;
};

export type URLProbeResponse = {
  title: string | null;
  duration_s: number | null;
  uploader: string | null;
  thumbnail_url: string | null;
  formats_available: number;
  resolvable: boolean;
  reason: string | null;
};
