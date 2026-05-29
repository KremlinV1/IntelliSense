import type { AlertSummary } from '../types/contracts';

/**
 * REST client for the confirmation service.
 *
 * All requests go through the Vite dev-server proxy at /api → :8080,
 * so we never hard-code the confirmation hostname here.
 */

const BASE = '/api';

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = 'ApiError';
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
  if (!res.ok) {
    throw new ApiError(res.status, `${res.status} ${res.statusText} on ${path}`);
  }
  return res.json() as Promise<T>;
}

export async function fetchRecentAlerts(limit = 50): Promise<AlertSummary[]> {
  return request<AlertSummary[]>(`/alerts/recent?limit=${limit}`);
}

export interface SearchFilters {
  cameraIds?: string[];
  dispositions?: string[];
  minScore?: number;
  since?: string; // ISO datetime
  until?: string;
  limit?: number;
  offset?: number;
}

export interface SearchResponse {
  filters: Record<string, unknown>;
  limit: number;
  offset: number;
  count: number;
  results: AlertSummary[];
}

export async function searchAlerts(f: SearchFilters = {}): Promise<SearchResponse> {
  // URLSearchParams serialises string[] as repeated params (camera_id=A&camera_id=B),
  // which is exactly what the FastAPI endpoint expects.
  const qs = new URLSearchParams();
  for (const id of f.cameraIds ?? []) qs.append('camera_id', id);
  for (const d of f.dispositions ?? []) qs.append('disposition', d);
  if (f.minScore !== undefined) qs.set('min_score', String(f.minScore));
  if (f.since) qs.set('since', f.since);
  if (f.until) qs.set('until', f.until);
  qs.set('limit', String(f.limit ?? 100));
  qs.set('offset', String(f.offset ?? 0));
  return request<SearchResponse>(`/alerts/search?${qs.toString()}`);
}

export async function confirmIncident(
  incidentId: string,
  operatorId: string,
): Promise<{ incident_id: string; status: string }> {
  // Backend expects `operator_id` as a query string arg (FastAPI treats
  // plain function parameters as query parameters by default).
  const qs = new URLSearchParams({ operator_id: operatorId });
  return request(`/alerts/${incidentId}/operator-confirm?${qs.toString()}`, {
    method: 'POST',
  });
}

