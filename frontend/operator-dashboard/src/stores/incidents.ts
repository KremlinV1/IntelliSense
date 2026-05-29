import { defineStore } from 'pinia';
import { ref, computed } from 'vue';

import { confirmIncident, fetchRecentAlerts } from '../api/alerts';
import { connectLiveMap, type ConnectionState } from '../api/live-map';
import type {
  AlertSummary,
  ConfirmedAlert,
  CrossCameraMatch,
} from '../types/contracts';

/**
 * Single source of truth for the operator console's live state.
 *
 * Dual-source design:
 *   - REST (fetchRecentAlerts on mount) loads the last N alerts so an
 *     operator opening the console sees history, not a blank screen.
 *   - WebSocket (live-map) streams new alerts and cross-camera handoffs.
 *
 * When both sources agree on an incident_id, the newer (WebSocket)
 * payload wins — it carries the full ConfirmedAlert shape.
 */

const MAX_ALERTS = 200;
const MAX_HANDOFFS = 100;

type AnyAlert = AlertSummary | ConfirmedAlert;

function normalise(a: AnyAlert): AlertSummary {
  // If we got a full ConfirmedAlert from the WS, flatten it into the
  // AlertSummary shape the UI renders. This way components only have to
  // handle one type.
  if ('frame_sha256' in a) {
    return {
      incident_id: a.incident_id,
      camera_id: a.camera.camera_id,
      disposition: a.disposition,
      threat_score: a.threat_score,
      threat_score_display: a.threat_score.toFixed(2),
      signal_breakdown: a.signal_breakdown,
      emitted_at: a.emitted_at ?? new Date().toISOString(),
      frame_uri: a.frame_uri,
      primary_detection: {
        class_name: a.primary_detection.class_name,
        threat_class: a.primary_detection.threat_class,
        confidence: a.primary_detection.confidence,
      },
      watchlist_hits: a.watchlist_hits.map((h) => ({
        identity_id: h.identity_id,
        identity_label: h.identity_label,
        similarity: h.similarity,
      })),
    };
  }
  return a;
}

export const useIncidentsStore = defineStore('incidents', () => {
  const alerts = ref<AlertSummary[]>([]);
  const handoffs = ref<CrossCameraMatch[]>([]);
  const connection = ref<ConnectionState>('closed');
  const loading = ref(false);
  const error = ref<string | null>(null);

  let client: ReturnType<typeof connectLiveMap> | null = null;

  function upsert(alert: AnyAlert) {
    const norm = normalise(alert);
    const existing = alerts.value.findIndex(
      (a) => a.incident_id === norm.incident_id,
    );
    if (existing >= 0) {
      alerts.value[existing] = norm;
    } else {
      alerts.value.unshift(norm);
      if (alerts.value.length > MAX_ALERTS) {
        alerts.value.length = MAX_ALERTS;
      }
    }
  }

  async function init() {
    loading.value = true;
    error.value = null;
    try {
      const recent = await fetchRecentAlerts(50);
      // fetchRecentAlerts returns newest-first.
      alerts.value = recent;
    } catch (err) {
      // Non-fatal. If the backend is simply offline the OFFLINE badge in the
      // header already communicates that — surfacing a red 500 toast on top
      // is redundant and alarming. Reserve the error banner for unexpected
      // responses (400s that aren't 404, malformed JSON, etc).
      const msg = (err as Error).message ?? '';
      const looksLikeOutage =
        msg.includes('500') ||
        msg.includes('502') ||
        msg.includes('503') ||
        msg.includes('504') ||
        msg.toLowerCase().includes('failed to fetch') ||
        msg.toLowerCase().includes('networkerror');
      if (!looksLikeOutage) {
        error.value = msg;
      } else {
        // Log for devtools so it isn't completely invisible.
        console.warn('[incidents] history fetch failed, backend likely offline:', msg);
      }
    } finally {
      loading.value = false;
    }

    client?.close();
    client = connectLiveMap({
      onStateChange: (s) => {
        connection.value = s;
      },
      onMessage: (env) => {
        if (env.type === 'alert') {
          upsert(env.data);
        } else if (env.type === 'handoff') {
          handoffs.value.unshift(env.data);
          if (handoffs.value.length > MAX_HANDOFFS) {
            handoffs.value.length = MAX_HANDOFFS;
          }
        }
      },
    });
  }

  function shutdown() {
    client?.close();
    client = null;
  }

  async function confirm(incidentId: string, operatorId: string) {
    await confirmIncident(incidentId, operatorId);
    // Optimistic local update. The backend will also re-emit via WS but
    // the operator shouldn't have to wait for a round-trip.
    const idx = alerts.value.findIndex((a) => a.incident_id === incidentId);
    if (idx >= 0) {
      alerts.value[idx] = {
        ...alerts.value[idx],
        disposition: 'auto_confirmed',
      };
    }
  }

  const reviewQueue = computed(() =>
    alerts.value.filter((a) => a.disposition === 'operator_review'),
  );
  const autoConfirmed = computed(() =>
    alerts.value.filter((a) => a.disposition === 'auto_confirmed'),
  );

  return {
    alerts,
    handoffs,
    connection,
    loading,
    error,
    reviewQueue,
    autoConfirmed,
    init,
    shutdown,
    confirm,
  };
});
