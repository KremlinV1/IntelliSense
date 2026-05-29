<script setup lang="ts">
import { computed } from 'vue';
import type { AlertSummary } from '../types/contracts';
import ThreatScoreBar from './ThreatScoreBar.vue';

const props = defineProps<{
  alert: AlertSummary;
  showConfirm?: boolean;
  busy?: boolean;
}>();

const emit = defineEmits<{
  (e: 'confirm', incidentId: string): void;
}>();

const disp = computed(() => {
  switch (props.alert.disposition) {
    case 'auto_confirmed':
      return { label: 'AUTO-CONFIRMED', badge: 'bg-threat-high/20 text-threat-high' };
    case 'operator_review':
      return { label: 'REVIEW', badge: 'bg-threat-med/20 text-threat-med' };
    case 'operator_dismissed':
      return { label: 'DISMISSED', badge: 'bg-slate-600/30 text-slate-400' };
    case 'suppressed':
      return { label: 'SUPPRESSED', badge: 'bg-slate-600/30 text-slate-400' };
  }
});

const emittedAt = computed(() => {
  const d = new Date(props.alert.emitted_at);
  return d.toLocaleTimeString(undefined, { hour12: false });
});

// Top signal: watchlist hit > corroboration > behaviour > persistence.
// This is the one-line rationale the operator sees first.
const topSignal = computed(() => {
  const sigs = props.alert.signal_breakdown;
  const entries = Object.entries(sigs).filter(([, v]) => v > 0);
  entries.sort((a, b) => b[1] - a[1]);
  if (!entries.length) return null;
  const [name, value] = entries[0];
  return { name: name.replace(/_/g, ' '), value };
});
</script>

<template>
  <article
    class="rounded-lg border border-ink-700 bg-ink-800/60 p-4 hover:bg-ink-800
           transition-colors"
  >
    <header class="flex items-start gap-3 mb-2">
      <div class="flex-1 min-w-0">
        <div class="flex items-center gap-2 text-xs font-mono">
          <span class="px-1.5 py-0.5 rounded" :class="disp.badge">{{ disp.label }}</span>
          <span class="text-slate-400">{{ emittedAt }}</span>
          <span class="text-slate-500">·</span>
          <span class="text-slate-300">{{ alert.camera_id }}</span>
        </div>
        <h3 class="mt-1 text-sm text-slate-200">
          {{ alert.primary_detection.class_name }}
          <span class="text-slate-500 font-mono">
            · {{ alert.primary_detection.confidence.toFixed(2) }}
          </span>
        </h3>
      </div>
      <div class="text-right shrink-0">
        <div class="text-lg font-mono text-slate-100">
          {{ alert.threat_score_display ?? alert.threat_score.toFixed(2) }}
        </div>
        <div class="text-[10px] uppercase tracking-wider text-slate-500">
          threat score
        </div>
      </div>
    </header>

    <ThreatScoreBar :score="alert.threat_score" />

    <div
      v-if="alert.watchlist_hits.length"
      class="mt-3 flex flex-wrap gap-1.5 text-xs"
    >
      <span
        v-for="h in alert.watchlist_hits"
        :key="h.identity_id"
        class="px-2 py-0.5 rounded bg-threat-critical/15 text-threat-critical
               font-mono border border-threat-critical/30"
      >
        ⚑ {{ h.identity_label }} · {{ (h.similarity * 100).toFixed(0) }}%
      </span>
    </div>

    <div
      v-if="topSignal"
      class="mt-2 text-xs text-slate-400 font-mono"
    >
      driver: <span class="text-slate-300">{{ topSignal.name }}</span>
      ({{ topSignal.value.toFixed(2) }})
    </div>

    <footer
      v-if="showConfirm && alert.disposition === 'operator_review'"
      class="mt-3 flex justify-end"
    >
      <button
        class="px-3 py-1.5 rounded-md text-xs font-mono uppercase tracking-wider
               bg-threat-high text-white hover:bg-threat-high/90
               disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        :disabled="busy"
        @click="emit('confirm', alert.incident_id)"
      >
        {{ busy ? 'confirming…' : 'confirm alert' }}
      </button>
    </footer>
  </article>
</template>
