/**
 * REST helpers for the sync ``POST /search`` endpoint.
 *
 * Search returns a ranked list of artifact references (one ``Analysis``
 * artifact's ``metadata.results``); the FE never sees the underlying
 * search ops or backends directly. Shape mirrors
 * ``media_engine/api/routes.py:SearchResponse``.
 */

import { api } from './client';

export type SearchMode = 'fulltext' | 'semantic' | 'hybrid';

export const SEARCH_MODES: readonly SearchMode[] = [
  'fulltext',
  'semantic',
  'hybrid',
];

export type SearchResultItem = {
  artifact_id: string;
  kind: string | null;
  score: number;
  snippet: string | null;
};

export type SearchResponse = {
  mode: SearchMode;
  query: string;
  top_k: number;
  results: SearchResultItem[];
};

export type SearchRequest = {
  mode: SearchMode;
  query: string;
  top_k: number;
  kind?: string;
  refresh?: boolean;
};

export function search(body: SearchRequest): Promise<SearchResponse> {
  return api.post<SearchResponse>('/search', body);
}
