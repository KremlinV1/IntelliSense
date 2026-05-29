import { defineStore } from 'pinia';
import { ref, computed } from 'vue';
import type { CameraRef } from '../types/contracts';
import {
  activateCamera,
  deactivateCamera,
  listCameras,
  upsertCamera,
  type CameraRecord,
  type CameraUpsertPayload,
} from '../api/cameras';
import { ApiError } from '../api/alerts';

/**
 * Camera registry.
 *
 * Three sources feed this store, in priority order:
 *   1. Postgres-backed /cameras API (Slice 2) — authoritative, admin-managed.
 *   2. Live event stream (CrossCameraMatch) — auto-discovers new cameras.
 *   3. A small built-in demo set — keeps the map non-empty when the backend
 *      has neither a DB nor events yet.
 *
 * The component API for readers (`cameras`, `list`) is stable; admin actions
 * (create/update/deactivate) go through the API and refresh the cache.
 */

const DEMO: Record<string, CameraRef> = {
  'CAM-A': {
    camera_id: 'CAM-A',
    latitude: 37.8084,
    longitude: -122.4098,
    label: 'Embarcadero · Pier 39',
    environment: 'road',
  },
  'CAM-B': {
    camera_id: 'CAM-B',
    latitude: 37.8058,
    longitude: -122.4170,
    label: 'Fisherman\'s Wharf',
    environment: 'public',
  },
  'CAM-C': {
    camera_id: 'CAM-C',
    latitude: 37.8024,
    longitude: -122.4058,
    label: 'Bay Bridge approach',
    environment: 'road',
  },
  'CAM-D': {
    camera_id: 'CAM-D',
    latitude: 37.8024,
    longitude: -122.4233,
    label: 'Ghirardelli Square',
    environment: 'public',
  },
};

export const useCamerasStore = defineStore('cameras', () => {
  const cameras = ref<Record<string, CameraRef>>({ ...DEMO });
  // Additional metadata only present when the DB-backed registry is online.
  // Admin UI uses these; map view ignores them.
  const records = ref<Record<string, CameraRecord>>({});
  const backendAvailable = ref<boolean>(false);
  const lastSyncError = ref<string | null>(null);

  function _applyRecord(rec: CameraRecord) {
    records.value[rec.camera_id] = rec;
    cameras.value[rec.camera_id] = {
      camera_id: rec.camera_id,
      latitude: rec.latitude,
      longitude: rec.longitude,
      label: rec.label ?? undefined,
      environment: rec.environment,
    };
  }

  function upsert(cam: CameraRef) {
    // Only overwrite coords if we don't already know better. The DB-backed
    // registry always wins; event-sourced CameraRefs often carry
    // latitude=0.0/longitude=0.0 placeholders.
    const existing = cameras.value[cam.camera_id];
    if (existing && existing.latitude !== 0) return;
    cameras.value[cam.camera_id] = { ...cam };
  }

  /** Load cameras from the REST API if available. 503 = no Postgres, which
   * is a valid dev-mode state — keep the demo seed and mark backend off. */
  async function hydrate() {
    try {
      const rows = await listCameras();
      records.value = {};
      for (const row of rows) _applyRecord(row);
      backendAvailable.value = true;
      lastSyncError.value = null;
    } catch (err) {
      backendAvailable.value = false;
      if (err instanceof ApiError && err.status === 503) {
        lastSyncError.value = null; // expected in dev mode
      } else {
        lastSyncError.value = (err as Error).message;
      }
    }
  }

  async function save(payload: CameraUpsertPayload): Promise<CameraRecord> {
    const rec = await upsertCamera(payload);
    _applyRecord(rec);
    return rec;
  }

  async function toggleActive(cameraId: string, next: boolean) {
    if (next) await activateCamera(cameraId);
    else await deactivateCamera(cameraId);
    const rec = records.value[cameraId];
    if (rec) _applyRecord({ ...rec, active: next });
  }

  const list = computed(() => Object.values(cameras.value));
  const adminList = computed(() =>
    Object.values(records.value).sort((a, b) => a.camera_id.localeCompare(b.camera_id)),
  );

  return {
    cameras,
    list,
    records,
    adminList,
    backendAvailable,
    lastSyncError,
    upsert,
    hydrate,
    save,
    toggleActive,
  };
});
