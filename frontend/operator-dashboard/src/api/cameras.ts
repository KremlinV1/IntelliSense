/**
 * Camera registry client.
 *
 * The backend returns 503 if Postgres isn't wired (dev mode without DB).
 * Callers should catch ApiError(503) and fall back to the static demo set.
 */
import { ApiError } from './alerts';

export interface CameraRecord {
  camera_id: string;
  label: string | null;
  environment: string;
  latitude: number;
  longitude: number;
  active: boolean;
  notes: string | null;
  created_at: string;
  updated_at: string;
  rtsp_url?: string;
}

export interface CameraUpsertPayload {
  camera_id: string;
  latitude: number;
  longitude: number;
  environment?: string;
  label?: string | null;
  rtsp_url?: string | null;
  notes?: string | null;
  active?: boolean;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    throw new ApiError(res.status, `${res.status} ${res.statusText} on ${path}`);
  }
  return res.json() as Promise<T>;
}

export async function listCameras(activeOnly = false): Promise<CameraRecord[]> {
  const qs = activeOnly ? '?active_only=true' : '';
  return request<CameraRecord[]>(`/cameras${qs}`);
}

export async function upsertCamera(payload: CameraUpsertPayload): Promise<CameraRecord> {
  return request<CameraRecord>(`/cameras`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function deactivateCamera(cameraId: string): Promise<void> {
  await request(`/cameras/${encodeURIComponent(cameraId)}/deactivate`, {
    method: 'POST',
  });
}

export async function activateCamera(cameraId: string): Promise<void> {
  await request(`/cameras/${encodeURIComponent(cameraId)}/activate`, {
    method: 'POST',
  });
}
