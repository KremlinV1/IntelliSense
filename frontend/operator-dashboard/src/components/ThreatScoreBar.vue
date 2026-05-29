<script setup lang="ts">
import { computed } from 'vue';

const props = defineProps<{
  score: number;        // 0..1
  autoConfirm?: number; // default 0.85
  reviewThreshold?: number; // default 0.55
}>();

// Visualise the score against the two decision thresholds so operators
// can see at a glance how close a borderline alert sits to auto-confirm.
// Never rounds up — matches format_score in the backend.
const autoConfirm = computed(() => props.autoConfirm ?? 0.85);
const review = computed(() => props.reviewThreshold ?? 0.55);

const pct = computed(() => Math.floor(props.score * 100));
const colour = computed(() => {
  if (props.score >= autoConfirm.value) return 'bg-threat-high';
  if (props.score >= review.value) return 'bg-threat-med';
  return 'bg-threat-low';
});
const reviewPct = computed(() => Math.floor(review.value * 100));
const autoPct = computed(() => Math.floor(autoConfirm.value * 100));
</script>

<template>
  <div class="relative w-full h-1.5 rounded-full bg-ink-700 overflow-visible">
    <div
      class="h-full rounded-full transition-[width] duration-500"
      :class="colour"
      :style="{ width: `${pct}%` }"
    />
    <!-- Threshold tick marks -->
    <div
      class="absolute top-[-2px] h-[10px] w-px bg-slate-500/60"
      :style="{ left: `${reviewPct}%` }"
      :title="`Operator review ≥ ${reviewPct}%`"
    />
    <div
      class="absolute top-[-2px] h-[10px] w-px bg-slate-400"
      :style="{ left: `${autoPct}%` }"
      :title="`Auto-confirm ≥ ${autoPct}%`"
    />
  </div>
</template>
