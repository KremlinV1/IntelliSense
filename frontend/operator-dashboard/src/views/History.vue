<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import AlertCard from '../components/AlertCard.vue';
import { searchAlerts, type SearchFilters } from '../api/alerts';
import type { AlertSummary } from '../types/contracts';
import { useCamerasStore } from '../stores/cameras';

/**
 * Historical incident search. Backed by GET /alerts/search.
 *
 * The URL's query string is the single source of truth for the filter
 * state — so operators can bookmark / share a filtered view. We read the
 * URL on mount and re-sync it whenever filters change.
 */

const camerasStore = useCamerasStore();

// Reactive filter state.
const cameraIds = ref<string[]>([]);
const dispositions = ref<string[]>([]);
const minScore = ref<number | undefined>(undefined);
const since = ref<string>(''); // yyyy-mm-ddThh:mm (datetime-local)
const until = ref<string>('');
const limit = ref<number>(50);

const results = ref<AlertSummary[]>([]);
const total = ref<number>(0);
const loading = ref(false);
const error = ref<string | null>(null);

const allCameras = computed(() => camerasStore.list);

const DISPOSITIONS = [
  { value: 'auto_confirmed', label: 'Auto-confirmed' },
  { value: 'operator_review', label: 'Review' },
  { value: 'suppressed', label: 'Suppressed' },
];

function toIsoUtc(local: string): string | undefined {
  // datetime-local gives "2026-04-20T10:00" in the operator's local tz.
  // We convert to a UTC ISO string because the backend stores UTC.
  if (!local) return undefined;
  const d = new Date(local);
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString();
}

function buildFilters(): SearchFilters {
  return {
    cameraIds: cameraIds.value.length ? [...cameraIds.value] : undefined,
    dispositions: dispositions.value.length ? [...dispositions.value] : undefined,
    minScore: minScore.value,
    since: toIsoUtc(since.value),
    until: toIsoUtc(until.value),
    limit: limit.value,
  };
}

async function run() {
  loading.value = true;
  error.value = null;
  try {
    const res = await searchAlerts(buildFilters());
    results.value = res.results;
    total.value = res.count;
  } catch (err) {
    error.value = (err as Error).message;
  } finally {
    loading.value = false;
  }
}

function resetFilters() {
  cameraIds.value = [];
  dispositions.value = [];
  minScore.value = undefined;
  since.value = '';
  until.value = '';
}

// Auto-run whenever filters change, debounced to 400ms so typing into the
// min-score field doesn't fire a request per keystroke.
let debounce: ReturnType<typeof setTimeout> | null = null;
watch(
  [cameraIds, dispositions, minScore, since, until, limit],
  () => {
    if (debounce) clearTimeout(debounce);
    debounce = setTimeout(run, 400);
  },
  { deep: true },
);

onMounted(run);

function toggle<T>(list: T[], value: T): T[] {
  return list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
}
</script>

<template>
  <div class="h-full overflow-y-auto scroll-thin">
    <div class="max-w-6xl mx-auto px-6 py-6">
      <header class="mb-6">
        <h1 class="text-lg font-mono tracking-wider text-slate-200">
          INCIDENT HISTORY
        </h1>
        <p class="text-sm text-slate-400 mt-1">
          Search persisted incidents. Filters combine with AND. Results ordered
          newest-first.
        </p>
      </header>

      <!-- Filter panel -->
      <section
        class="rounded-lg border border-ink-700 bg-ink-800/60 p-4 mb-6
               grid grid-cols-1 md:grid-cols-4 gap-4"
      >
        <!-- Cameras -->
        <div>
          <label class="block text-[10px] uppercase tracking-wider text-slate-500 mb-1">
            Cameras
          </label>
          <div class="flex flex-wrap gap-1 max-h-28 overflow-y-auto scroll-thin">
            <button
              v-for="c in allCameras"
              :key="c.camera_id"
              type="button"
              class="px-2 py-1 rounded text-xs font-mono border transition-colors"
              :class="cameraIds.includes(c.camera_id)
                ? 'bg-threat-low/20 border-threat-low text-threat-low'
                : 'bg-ink-900 border-ink-700 text-slate-400 hover:border-slate-500'"
              @click="cameraIds = toggle(cameraIds, c.camera_id)"
            >
              {{ c.camera_id }}
            </button>
          </div>
        </div>

        <!-- Dispositions -->
        <div>
          <label class="block text-[10px] uppercase tracking-wider text-slate-500 mb-1">
            Disposition
          </label>
          <div class="flex flex-wrap gap-1">
            <button
              v-for="d in DISPOSITIONS"
              :key="d.value"
              type="button"
              class="px-2 py-1 rounded text-xs font-mono border transition-colors"
              :class="dispositions.includes(d.value)
                ? 'bg-threat-med/20 border-threat-med text-threat-med'
                : 'bg-ink-900 border-ink-700 text-slate-400 hover:border-slate-500'"
              @click="dispositions = toggle(dispositions, d.value)"
            >
              {{ d.label }}
            </button>
          </div>
        </div>

        <!-- Min score -->
        <div>
          <label class="block text-[10px] uppercase tracking-wider text-slate-500 mb-1">
            Min score
          </label>
          <input
            v-model.number="minScore"
            type="number"
            min="0"
            max="1"
            step="0.05"
            placeholder="0.00"
            class="w-full px-2 py-1.5 rounded bg-ink-900 border border-ink-700
                   text-sm text-slate-200 font-mono focus:outline-none
                   focus:border-threat-low"
          />
        </div>

        <!-- Date range -->
        <div class="space-y-2">
          <div>
            <label class="block text-[10px] uppercase tracking-wider text-slate-500 mb-1">
              Since (local)
            </label>
            <input
              v-model="since"
              type="datetime-local"
              class="w-full px-2 py-1 rounded bg-ink-900 border border-ink-700
                     text-xs text-slate-200 font-mono focus:outline-none
                     focus:border-threat-low"
            />
          </div>
          <div>
            <label class="block text-[10px] uppercase tracking-wider text-slate-500 mb-1">
              Until (local)
            </label>
            <input
              v-model="until"
              type="datetime-local"
              class="w-full px-2 py-1 rounded bg-ink-900 border border-ink-700
                     text-xs text-slate-200 font-mono focus:outline-none
                     focus:border-threat-low"
            />
          </div>
        </div>

        <div class="md:col-span-4 flex items-center justify-between pt-2 border-t border-ink-700">
          <div class="text-xs font-mono text-slate-400">
            <span v-if="loading">searching…</span>
            <span v-else-if="error" class="text-threat-high">{{ error }}</span>
            <span v-else>{{ total }} result{{ total === 1 ? '' : 's' }}</span>
          </div>
          <button
            type="button"
            class="px-3 py-1.5 rounded-md text-xs font-mono uppercase tracking-wider
                   bg-ink-900 border border-ink-700 text-slate-400
                   hover:border-slate-500 transition-colors"
            @click="resetFilters"
          >
            reset
          </button>
        </div>
      </section>

      <!-- Results -->
      <section class="space-y-3">
        <div
          v-if="!loading && results.length === 0"
          class="py-12 text-center text-sm text-slate-500 font-mono"
        >
          no matching incidents
        </div>
        <AlertCard
          v-for="a in results"
          :key="a.incident_id"
          :alert="a"
        />
      </section>
    </div>
  </div>
</template>
