<script setup lang="ts">
import { computed, watch } from 'vue';
import AlertCard from '../components/AlertCard.vue';
import CameraMap from '../components/CameraMap.vue';
import { useCamerasStore } from '../stores/cameras';
import { useIncidentsStore } from '../stores/incidents';

const incidents = useIncidentsStore();
const cameras = useCamerasStore();

// Learn camera coordinates from any handoff we see. In dev the demo
// registry already knows CAM-A/B/C/D, but this keeps the map self-extending.
watch(
  () => incidents.handoffs,
  (list) => {
    for (const h of list) {
      cameras.upsert(h.prior_camera);
      cameras.upsert(h.new_camera);
    }
  },
  { deep: true },
);

// Top 8 alerts — chronological, newest first.
const feed = computed(() => incidents.alerts.slice(0, 8));
</script>

<template>
  <div class="flex h-full min-h-0">
    <!-- Left: map -->
    <section class="flex-1 min-w-0 relative">
      <CameraMap
        :cameras="cameras.list"
        :handoffs="incidents.handoffs"
        :alerts="incidents.alerts"
      />

      <!-- Map overlay: last cross-camera handoff, most-prominent signal for
           "a target is moving through the network right now" -->
      <div
        v-if="incidents.handoffs.length"
        class="absolute top-4 left-4 max-w-xs rounded-md bg-ink-800/90
               backdrop-blur border border-ink-700 p-3 font-mono text-xs"
      >
        <div class="text-threat-critical uppercase tracking-wider mb-1">
          last handoff
        </div>
        <div class="text-slate-200">
          {{ incidents.handoffs[0].prior_camera.camera_id }}
          → {{ incidents.handoffs[0].new_camera.camera_id }}
        </div>
        <div class="text-slate-400 mt-1">
          sim {{ (incidents.handoffs[0].similarity * 100).toFixed(0) }}%
          · {{ incidents.handoffs[0].elapsed_seconds.toFixed(1) }}s gap
        </div>
      </div>
    </section>

    <!-- Right: live alert feed -->
    <aside
      class="w-[28rem] shrink-0 border-l border-ink-700 bg-ink-900/80
             flex flex-col min-h-0"
    >
      <header class="px-4 py-3 border-b border-ink-700 flex items-center justify-between">
        <h2 class="text-sm font-mono tracking-wider text-slate-300">LIVE FEED</h2>
        <span class="text-xs font-mono text-slate-500">
          {{ incidents.alerts.length }} alerts
        </span>
      </header>

      <div class="flex-1 min-h-0 overflow-y-auto scroll-thin p-3 space-y-2">
        <div v-if="incidents.loading" class="text-center text-xs text-slate-500 py-8">
          loading recent alerts…
        </div>
        <div
          v-else-if="feed.length === 0"
          class="text-center text-xs text-slate-500 py-8 font-mono"
        >
          no alerts yet · {{ incidents.connection === 'open' ? 'connected · waiting' : 'offline' }}
        </div>
        <AlertCard
          v-for="a in feed"
          :key="a.incident_id"
          :alert="a"
        />
      </div>
    </aside>
  </div>
</template>
