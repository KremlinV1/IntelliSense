<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import { useCamerasStore } from '../stores/cameras';
import type { CameraUpsertPayload } from '../api/cameras';
import { ApiError } from '../api/alerts';

/**
 * Camera registry admin. Operator-only (no auth in the POC — gateway
 * enforces that in production).
 *
 * Layout: table of existing cameras on the left, edit form on the right.
 * Clicking a row loads it into the form; "New camera" clears the form.
 * Save hits POST /cameras which upserts, so the same form handles both
 * create and update without mode flags.
 */

const cameras = useCamerasStore();

const ENVIRONMENTS = [
  { value: 'public', label: 'Public' },
  { value: 'road', label: 'Road' },
  { value: 'retail', label: 'Retail' },
  { value: 'hospitality', label: 'Hospitality' },
  { value: 'residential', label: 'Residential' },
];

interface FormState {
  camera_id: string;
  label: string;
  environment: string;
  latitude: string;
  longitude: string;
  rtsp_url: string;
  notes: string;
  active: boolean;
  is_new: boolean;
}

function blankForm(): FormState {
  return {
    camera_id: '',
    label: '',
    environment: 'public',
    latitude: '',
    longitude: '',
    rtsp_url: '',
    notes: '',
    active: true,
    is_new: true,
  };
}

const form = reactive<FormState>(blankForm());
const saving = ref(false);
const saveError = ref<string | null>(null);
const saveMessage = ref<string | null>(null);

const backendOff = computed(() => !cameras.backendAvailable);

onMounted(() => {
  void cameras.hydrate();
});

function editCamera(cameraId: string) {
  const rec = cameras.records[cameraId];
  if (!rec) return;
  Object.assign(form, {
    camera_id: rec.camera_id,
    label: rec.label ?? '',
    environment: rec.environment,
    latitude: String(rec.latitude),
    longitude: String(rec.longitude),
    rtsp_url: rec.rtsp_url ?? '',
    notes: rec.notes ?? '',
    active: rec.active,
    is_new: false,
  });
  saveMessage.value = null;
  saveError.value = null;
}

function startNew() {
  Object.assign(form, blankForm());
  saveMessage.value = null;
  saveError.value = null;
}

function validate(): string | null {
  if (!form.camera_id.trim()) return 'camera_id is required';
  const lat = Number(form.latitude);
  const lon = Number(form.longitude);
  if (!Number.isFinite(lat) || lat < -90 || lat > 90) return 'latitude must be in [-90, 90]';
  if (!Number.isFinite(lon) || lon < -180 || lon > 180) return 'longitude must be in [-180, 180]';
  return null;
}

async function save() {
  saveError.value = null;
  saveMessage.value = null;
  const validationMsg = validate();
  if (validationMsg) {
    saveError.value = validationMsg;
    return;
  }
  const payload: CameraUpsertPayload = {
    camera_id: form.camera_id.trim(),
    latitude: Number(form.latitude),
    longitude: Number(form.longitude),
    environment: form.environment,
    label: form.label.trim() || null,
    rtsp_url: form.rtsp_url.trim() || null,
    notes: form.notes.trim() || null,
    active: form.active,
  };
  saving.value = true;
  try {
    await cameras.save(payload);
    saveMessage.value = `saved ${payload.camera_id}`;
    form.is_new = false;
  } catch (err) {
    if (err instanceof ApiError) {
      saveError.value = `${err.status}: ${err.message}`;
    } else {
      saveError.value = (err as Error).message;
    }
  } finally {
    saving.value = false;
  }
}

async function toggle(cameraId: string, next: boolean) {
  try {
    await cameras.toggleActive(cameraId, next);
  } catch (err) {
    saveError.value = (err as Error).message;
  }
}
</script>

<template>
  <div class="h-full overflow-y-auto scroll-thin">
    <div class="max-w-7xl mx-auto px-6 py-6">
      <header class="mb-6 flex items-start justify-between">
        <div>
          <h1 class="text-lg font-mono tracking-wider text-slate-200">
            CAMERA REGISTRY
          </h1>
          <p class="text-sm text-slate-400 mt-1">
            Register new cameras or update existing ones. Changes take effect
            on the next capture-service reload.
          </p>
        </div>
        <button
          type="button"
          class="px-3 py-1.5 rounded-md text-xs font-mono uppercase tracking-wider
                 bg-threat-low/10 border border-threat-low/50 text-threat-low
                 hover:bg-threat-low/20 transition-colors"
          @click="startNew"
        >
          + new camera
        </button>
      </header>

      <!-- Backend-off banner. Admin endpoints need Postgres. -->
      <div
        v-if="backendOff"
        class="mb-4 rounded-md border border-threat-med/40 bg-threat-med/10
               px-4 py-3 text-xs font-mono text-threat-med"
      >
        Camera registry requires Postgres. Start the confirmation service with
        POSTGRES_HOST set (or run the full docker-compose stack). The map view
        continues to work from demo seeds + live events.
      </div>

      <div class="grid grid-cols-1 lg:grid-cols-5 gap-6">
        <!-- Camera list -->
        <section class="lg:col-span-3 rounded-lg border border-ink-700 bg-ink-800/60 overflow-hidden">
          <table class="w-full text-xs font-mono">
            <thead>
              <tr class="text-left text-[10px] uppercase tracking-wider text-slate-500 border-b border-ink-700">
                <th class="px-3 py-2">preview</th>
                <th class="px-3 py-2">camera_id</th>
                <th class="px-3 py-2">label</th>
                <th class="px-3 py-2">env</th>
                <th class="px-3 py-2">lat, lon</th>
                <th class="px-3 py-2">status</th>
                <th class="px-3 py-2"></th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-if="cameras.adminList.length === 0 && !backendOff"
                class="text-slate-500"
              >
                <td colspan="7" class="px-3 py-6 text-center">
                  no cameras registered yet — create one →
                </td>
              </tr>
              <tr
                v-for="cam in cameras.adminList"
                :key="cam.camera_id"
                class="border-b border-ink-700/60 hover:bg-ink-700/40 transition-colors cursor-pointer"
                :class="{ 'opacity-50': !cam.active }"
                @click="editCamera(cam.camera_id)"
              >
                <td class="px-3 py-2">
                  <img 
                    :src="`http://127.0.0.1:8080/cameras/${cam.camera_id}/preview.jpg`"
                    :alt="`Preview for ${cam.camera_id}`"
                    class="w-16 h-12 object-cover rounded border border-ink-600"
                    @error="$event.target.style.display='none'"
                    loading="lazy"
                  />
                </td>
                <td class="px-3 py-2 text-slate-200">{{ cam.camera_id }}</td>
                <td class="px-3 py-2 text-slate-400">{{ cam.label ?? '—' }}</td>
                <td class="px-3 py-2 text-slate-400">{{ cam.environment }}</td>
                <td class="px-3 py-2 text-slate-400">
                  {{ cam.latitude.toFixed(4) }}, {{ cam.longitude.toFixed(4) }}
                </td>
                <td class="px-3 py-2">
                  <span
                    :class="cam.active
                      ? 'text-threat-low'
                      : 'text-slate-500'"
                  >
                    {{ cam.active ? 'active' : 'inactive' }}
                  </span>
                </td>
                <td class="px-3 py-2 text-right">
                  <button
                    type="button"
                    class="text-[10px] uppercase tracking-wider text-slate-500 hover:text-slate-300"
                    @click.stop="toggle(cam.camera_id, !cam.active)"
                  >
                    {{ cam.active ? 'disable' : 'enable' }}
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </section>

        <!-- Edit form -->
        <section class="lg:col-span-2 rounded-lg border border-ink-700 bg-ink-800/60 p-4">
          <h2 class="text-xs font-mono uppercase tracking-wider text-slate-400 mb-3">
            {{ form.is_new ? 'new camera' : `edit ${form.camera_id}` }}
          </h2>

          <form class="space-y-3" @submit.prevent="save">
            <div>
              <label class="block text-[10px] uppercase tracking-wider text-slate-500 mb-1">
                camera_id
              </label>
              <input
                v-model="form.camera_id"
                type="text"
                :disabled="!form.is_new"
                placeholder="CAM-STORE-01"
                class="w-full px-2 py-1.5 rounded bg-ink-900 border border-ink-700
                       text-sm text-slate-200 font-mono focus:outline-none
                       focus:border-threat-low disabled:opacity-60"
              />
            </div>

            <div>
              <label class="block text-[10px] uppercase tracking-wider text-slate-500 mb-1">
                label
              </label>
              <input
                v-model="form.label"
                type="text"
                placeholder="Main entrance"
                class="w-full px-2 py-1.5 rounded bg-ink-900 border border-ink-700
                       text-sm text-slate-200 focus:outline-none focus:border-threat-low"
              />
            </div>

            <div>
              <label class="block text-[10px] uppercase tracking-wider text-slate-500 mb-1">
                environment
              </label>
              <select
                v-model="form.environment"
                class="w-full px-2 py-1.5 rounded bg-ink-900 border border-ink-700
                       text-sm text-slate-200 font-mono focus:outline-none
                       focus:border-threat-low"
              >
                <option v-for="e in ENVIRONMENTS" :key="e.value" :value="e.value">
                  {{ e.label }}
                </option>
              </select>
            </div>

            <div class="grid grid-cols-2 gap-2">
              <div>
                <label class="block text-[10px] uppercase tracking-wider text-slate-500 mb-1">
                  latitude
                </label>
                <input
                  v-model="form.latitude"
                  type="number"
                  step="0.000001"
                  placeholder="37.8084"
                  class="w-full px-2 py-1.5 rounded bg-ink-900 border border-ink-700
                         text-sm text-slate-200 font-mono focus:outline-none
                         focus:border-threat-low"
                />
              </div>
              <div>
                <label class="block text-[10px] uppercase tracking-wider text-slate-500 mb-1">
                  longitude
                </label>
                <input
                  v-model="form.longitude"
                  type="number"
                  step="0.000001"
                  placeholder="-122.4098"
                  class="w-full px-2 py-1.5 rounded bg-ink-900 border border-ink-700
                         text-sm text-slate-200 font-mono focus:outline-none
                         focus:border-threat-low"
                />
              </div>
            </div>

            <div>
              <label class="block text-[10px] uppercase tracking-wider text-slate-500 mb-1">
                rtsp_url
                <span class="text-slate-600 normal-case lowercase"> · optional, redacted in list responses</span>
              </label>
              <input
                v-model="form.rtsp_url"
                type="text"
                placeholder="rtsp://192.168.1.50:554/stream"
                class="w-full px-2 py-1.5 rounded bg-ink-900 border border-ink-700
                       text-sm text-slate-200 font-mono focus:outline-none
                       focus:border-threat-low"
              />
            </div>

            <div>
              <label class="block text-[10px] uppercase tracking-wider text-slate-500 mb-1">
                notes
              </label>
              <textarea
                v-model="form.notes"
                rows="2"
                class="w-full px-2 py-1.5 rounded bg-ink-900 border border-ink-700
                       text-sm text-slate-200 focus:outline-none focus:border-threat-low resize-none"
              />
            </div>

            <label class="flex items-center gap-2 text-xs text-slate-400 font-mono">
              <input v-model="form.active" type="checkbox" class="accent-threat-low" />
              active
            </label>

            <div class="pt-2 flex items-center justify-between gap-3 border-t border-ink-700">
              <div class="text-[11px] font-mono min-h-[1rem]">
                <span v-if="saveError" class="text-threat-high">{{ saveError }}</span>
                <span v-else-if="saveMessage" class="text-threat-low">{{ saveMessage }}</span>
                <span v-else-if="backendOff" class="text-threat-med">
                  backend offline — cannot save
                </span>
              </div>
              <button
                type="submit"
                :disabled="saving || backendOff"
                :title="backendOff
                  ? 'Camera registry requires Postgres. See banner above.'
                  : ''"
                class="px-4 py-1.5 rounded-md text-xs font-mono uppercase tracking-wider
                       transition-colors border"
                :class="backendOff
                  ? 'bg-ink-900 border-ink-700 text-slate-600 cursor-not-allowed'
                  : 'bg-threat-low/10 border-threat-low/50 text-threat-low hover:bg-threat-low/20'"
              >
                {{ saving ? 'saving…' : form.is_new ? 'create' : 'save' }}
              </button>
            </div>
          </form>
        </section>
      </div>
    </div>
  </div>
</template>
