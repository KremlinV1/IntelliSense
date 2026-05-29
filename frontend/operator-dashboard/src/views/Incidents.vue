<script setup lang="ts">
import { ref } from 'vue';
import AlertCard from '../components/AlertCard.vue';
import { useIncidentsStore } from '../stores/incidents';

const incidents = useIncidentsStore();

// Operator identity — in production this comes from an OIDC token. For
// the dev dashboard we persist it in localStorage so refreshes don't blank
// it out. Entering an operator id is required before any confirm action
// fires — enforcing charter constraint #2 (no automated dispatch without
// a logged human confirmation).
const operatorId = ref(localStorage.getItem('veilwatch.operator_id') ?? '');
const busyId = ref<string | null>(null);
const errorMsg = ref<string | null>(null);

function saveOperator() {
  localStorage.setItem('veilwatch.operator_id', operatorId.value.trim());
}

async function onConfirm(incidentId: string) {
  const op = operatorId.value.trim();
  if (!op) {
    errorMsg.value = 'Enter your operator ID before confirming an alert.';
    return;
  }
  errorMsg.value = null;
  busyId.value = incidentId;
  try {
    await incidents.confirm(incidentId, op);
  } catch (err) {
    errorMsg.value = `Failed to confirm: ${(err as Error).message}`;
  } finally {
    busyId.value = null;
  }
}
</script>

<template>
  <div class="h-full overflow-y-auto scroll-thin">
    <div class="max-w-4xl mx-auto px-6 py-6 space-y-6">
      <header class="flex items-end gap-4">
        <div class="flex-1">
          <h1 class="text-lg font-mono tracking-wider text-slate-200">
            INCIDENT QUEUE
          </h1>
          <p class="text-sm text-slate-400 mt-1">
            Alerts awaiting human confirmation.
            Constraint #2: <span class="text-slate-300">no dispatch fires without
            a logged operator confirmation.</span>
          </p>
        </div>
        <div>
          <label class="block text-[10px] uppercase tracking-wider text-slate-500 mb-1">
            Operator ID
          </label>
          <input
            v-model="operatorId"
            @blur="saveOperator"
            placeholder="e.g. ops-alice"
            class="px-3 py-1.5 rounded-md bg-ink-800 border border-ink-700
                   font-mono text-sm text-slate-200 focus:outline-none
                   focus:border-threat-low"
          />
        </div>
      </header>

      <div
        v-if="errorMsg"
        class="px-3 py-2 rounded-md bg-threat-high/10 border border-threat-high/40
               text-sm text-threat-high"
      >
        {{ errorMsg }}
      </div>

      <section>
        <h2 class="text-xs uppercase tracking-wider text-slate-500 mb-2">
          Awaiting Review ({{ incidents.reviewQueue.length }})
        </h2>
        <div class="space-y-3">
          <div
            v-if="incidents.reviewQueue.length === 0"
            class="py-8 text-center text-sm text-slate-500 font-mono"
          >
            no alerts awaiting review
          </div>
          <AlertCard
            v-for="a in incidents.reviewQueue"
            :key="a.incident_id"
            :alert="a"
            :show-confirm="true"
            :busy="busyId === a.incident_id"
            @confirm="onConfirm"
          />
        </div>
      </section>

      <section v-if="incidents.autoConfirmed.length">
        <h2 class="text-xs uppercase tracking-wider text-slate-500 mb-2">
          Recent Auto-Confirmed ({{ incidents.autoConfirmed.length }})
        </h2>
        <div class="space-y-3">
          <AlertCard
            v-for="a in incidents.autoConfirmed.slice(0, 10)"
            :key="a.incident_id"
            :alert="a"
          />
        </div>
      </section>
    </div>
  </div>
</template>
