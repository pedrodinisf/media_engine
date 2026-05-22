/**
 * Bearer-token store + persistence.
 *
 * The Phase-6 v1 auth flow is paste-token (plan §1, §13.2):
 *   1. User runs `med api token create --label web-ui` in their shell.
 *   2. UI's /ui/setup route asks for the secret + "remember me" choice.
 *   3. Token lives in localStorage (default) or sessionStorage (per-tab).
 *
 * Both stores share the same key. On a fresh load we check localStorage
 * first, then sessionStorage; whichever has a value wins. Clearing
 * removes it from both.
 *
 * The token is read by the REST client (commits 41+) and is the SSE
 * ?token= query-param source (commit 43). XSS-readable by design —
 * the v1.x hardening path is an httpOnly cookie session (plan §13.2).
 */

import { writable } from 'svelte/store';
import { browser } from '$app/environment';

const STORAGE_KEY = 'media_engine:bearer';

type Persistence = 'local' | 'session';

function loadToken(): string | null {
  if (!browser) return null;
  return window.localStorage.getItem(STORAGE_KEY) ?? window.sessionStorage.getItem(STORAGE_KEY);
}

export const token = writable<string | null>(loadToken());

export function setToken(value: string, persistence: Persistence): void {
  if (!browser) return;
  // Clear the alternate store so we don't end up with the secret in
  // two places (and stale on a refresh after the user flips the toggle).
  const primary = persistence === 'local' ? window.localStorage : window.sessionStorage;
  const secondary = persistence === 'local' ? window.sessionStorage : window.localStorage;
  primary.setItem(STORAGE_KEY, value);
  secondary.removeItem(STORAGE_KEY);
  token.set(value);
}

export function clearToken(): void {
  if (!browser) return;
  window.localStorage.removeItem(STORAGE_KEY);
  window.sessionStorage.removeItem(STORAGE_KEY);
  token.set(null);
}

/** Sync access for the REST client + SSE adapter. */
export function getTokenSync(): string | null {
  return loadToken();
}
