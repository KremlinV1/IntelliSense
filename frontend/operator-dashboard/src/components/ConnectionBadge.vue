<script setup lang="ts">
import { computed } from 'vue';
import type { ConnectionState } from '../api/live-map';

const props = defineProps<{ state: ConnectionState }>();

const meta = computed(() => {
  switch (props.state) {
    case 'open':
      return { label: 'LIVE', color: 'text-ok', dot: 'bg-ok' };
    case 'connecting':
      return { label: 'CONNECTING', color: 'text-threat-med', dot: 'bg-threat-med' };
    case 'closed':
      return { label: 'OFFLINE', color: 'text-slate-400', dot: 'bg-slate-500' };
    case 'error':
      return { label: 'ERROR', color: 'text-threat-high', dot: 'bg-threat-high' };
  }
});
</script>

<template>
  <div class="flex items-center gap-2 font-mono text-xs tracking-wider" :class="meta.color">
    <span class="relative w-2 h-2 rounded-full" :class="meta.dot">
      <span
        v-if="state === 'open'"
        class="pulse-ring absolute inset-0 rounded-full"
      />
    </span>
    {{ meta.label }}
  </div>
</template>
