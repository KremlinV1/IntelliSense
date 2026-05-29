<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue';
import L from 'leaflet';

import type { AlertSummary, CameraRef, CrossCameraMatch } from '../types/contracts';

const props = defineProps<{
  cameras: CameraRef[];
  handoffs: CrossCameraMatch[];
  alerts: AlertSummary[];
}>();

const mapEl = ref<HTMLDivElement | null>(null);
let map: L.Map | null = null;
let markerLayer: L.LayerGroup | null = null;
let handoffLayer: L.LayerGroup | null = null;

/**
 * Marker design decisions:
 *   - CircleMarkers instead of image-icons: theme-matching colour is one CSS
 *     variable, and there's no extra HTTP round-trip for an image sprite.
 *   - Camera markers are cyan; handoff markers fade from red (new) to dim
 *     (old) over ~30s. This gives the operator a visual "breadcrumb" of the
 *     last few minutes of cross-camera activity without clutter.
 */

function drawCameras() {
  if (!map) return;
  markerLayer?.clearLayers();
  markerLayer = L.layerGroup().addTo(map);

  for (const cam of props.cameras) {
    // Skip placeholder 0,0 coords — those are from test events without
    // real geography.
    if (cam.latitude === 0 && cam.longitude === 0) continue;

    const hasAlert = props.alerts.some(
      (a) => a.camera_id === cam.camera_id &&
             (a.disposition === 'auto_confirmed' || a.disposition === 'operator_review'),
    );

    const marker = L.circleMarker([cam.latitude, cam.longitude], {
      radius: 8,
      color: hasAlert ? '#ef4444' : '#22d3ee',
      fillColor: hasAlert ? '#ef4444' : '#22d3ee',
      fillOpacity: hasAlert ? 0.9 : 0.5,
      weight: 2,
    });

    marker.bindPopup(`
      <div style="font-family: monospace; font-size: 12px;">
        <div style="color: #cbd5e1; font-weight: 600;">${cam.camera_id}</div>
        <div style="color: #94a3b8;">${cam.label ?? ''}</div>
        <div style="color: #64748b; font-size: 10px;">${cam.environment ?? ''}</div>
      </div>
    `);
    marker.addTo(markerLayer);
  }
}

function drawHandoffs() {
  if (!map) return;
  handoffLayer?.clearLayers();
  handoffLayer = L.layerGroup().addTo(map);

  // Only the last 10 handoffs, newest first.
  const recent = props.handoffs.slice(0, 10);
  const now = Date.now();

  for (let i = 0; i < recent.length; i++) {
    const h = recent[i];
    const from = props.cameras.find((c) => c.camera_id === h.prior_camera.camera_id);
    const to = props.cameras.find((c) => c.camera_id === h.new_camera.camera_id);
    if (!from || !to) continue;
    if ((from.latitude === 0 && from.longitude === 0)
        || (to.latitude === 0 && to.longitude === 0)) continue;

    // Age the line: newest = full opacity, older = faded. This caps at 10
    // so even without a timestamp on every handoff the visual decay reads
    // naturally.
    const ageFactor = 1 - i / 10;
    const ts = h.emitted_at ? new Date(h.emitted_at).getTime() : now;
    const secondsOld = Math.max(0, (now - ts) / 1000);
    const timeDecay = Math.max(0.2, 1 - secondsOld / 300); // 5 min horizon
    const opacity = ageFactor * timeDecay;

    const line = L.polyline(
      [[from.latitude, from.longitude], [to.latitude, to.longitude]],
      {
        color: '#ec4899',
        weight: 2,
        opacity,
        dashArray: i === 0 ? undefined : '4 4', // newest solid, rest dashed
      },
    );
    line.bindTooltip(
      `<span style="font-family:monospace;font-size:11px;">` +
      `${h.prior_camera.camera_id} → ${h.new_camera.camera_id} ` +
      `· sim ${(h.similarity * 100).toFixed(0)}% ` +
      `· ${h.elapsed_seconds.toFixed(1)}s</span>`,
    );
    line.addTo(handoffLayer);
  }
}

onMounted(() => {
  if (!mapEl.value) return;

  map = L.map(mapEl.value, {
    zoomControl: true,
    attributionControl: true,
    preferCanvas: true,
  });

  // OSM tiles: no API key, adequate for dev. Swap to Mapbox GL later.
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© OpenStreetMap',
    maxZoom: 19,
  }).addTo(map);

  // Fit bounds around known cameras; fall back to SF if empty.
  const known = props.cameras.filter((c) => c.latitude !== 0 || c.longitude !== 0);
  if (known.length) {
    const bounds = L.latLngBounds(known.map((c) => [c.latitude, c.longitude]));
    map.fitBounds(bounds.pad(0.3));
  } else {
    map.setView([37.8, -122.41], 14);
  }

  drawCameras();
  drawHandoffs();
});

onBeforeUnmount(() => {
  map?.remove();
  map = null;
});

// Redraw when inputs change. Cheap — we clear + re-add a small number of
// layers rather than diffing.
watch(() => props.cameras, drawCameras, { deep: true });
watch(() => props.alerts, drawCameras, { deep: true });
watch(() => props.handoffs, drawHandoffs, { deep: true });
</script>

<template>
  <div ref="mapEl" class="w-full h-full" />
</template>
