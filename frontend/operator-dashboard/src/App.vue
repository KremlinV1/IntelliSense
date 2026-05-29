<script setup lang="ts">
import { onBeforeUnmount, onMounted } from 'vue';

import ConnectionBadge from './components/ConnectionBadge.vue';
import { useIncidentsStore } from './stores/incidents';

// Init on mount, shut the WebSocket down on unmount so HMR doesn't pile up
// zombie connections during development.
const store = useIncidentsStore();
onMounted(() => store.init());
onBeforeUnmount(() => store.shutdown());
</script>

<template>
  <div class="h-screen flex flex-col overflow-hidden">
    <header
      class="flex items-center gap-6 px-6 py-3 border-b border-ink-700 bg-ink-800/70 backdrop-blur"
    >
      <div class="flex items-center gap-2">
        <div class="w-2.5 h-2.5 rounded-full bg-threat-high" />
        <span class="font-mono text-sm tracking-wider text-slate-300">
          VEILWATCH&nbsp;·&nbsp;OPERATOR
        </span>
      </div>

      <nav class="flex items-center gap-1 ml-4 text-sm">
        <router-link
          to="/live"
          class="px-3 py-1.5 rounded-md hover:bg-ink-700 transition-colors"
          active-class="bg-ink-700 text-white"
        >
          Live Map
        </router-link>
        <router-link
          to="/incidents"
          class="px-3 py-1.5 rounded-md hover:bg-ink-700 transition-colors"
          active-class="bg-ink-700 text-white"
        >
          Incident Queue
          <span
            v-if="store.reviewQueue.length"
            class="ml-2 px-1.5 py-0.5 rounded bg-threat-med/20 text-threat-med text-xs font-mono"
          >
            {{ store.reviewQueue.length }}
          </span>
        </router-link>
        <router-link
          to="/history"
          class="px-3 py-1.5 rounded-md hover:bg-ink-700 transition-colors"
          active-class="bg-ink-700 text-white"
        >
          History
        </router-link>
        <router-link
          to="/cameras"
          class="px-3 py-1.5 rounded-md hover:bg-ink-700 transition-colors"
          active-class="bg-ink-700 text-white"
        >
          Cameras
        </router-link>
      </nav>

      <div class="ml-auto flex items-center gap-4 text-xs text-slate-400">
        <ConnectionBadge :state="store.connection" />
      </div>
    </header>

    <main class="flex-1 min-h-0">
      <router-view />
    </main>

    <div
      v-if="store.error"
      class="fixed bottom-4 right-4 max-w-md px-4 py-2 rounded-md bg-threat-high/20
             border border-threat-high/40 text-sm text-threat-high"
    >
      {{ store.error }}
    </div>
  </div>
</template>
