import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router';

import LiveMap from './views/LiveMap.vue';
import Incidents from './views/Incidents.vue';
import History from './views/History.vue';
import Cameras from './views/Cameras.vue';

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/live' },
  { path: '/live', name: 'live', component: LiveMap, meta: { title: 'Live Map' } },
  {
    path: '/incidents',
    name: 'incidents',
    component: Incidents,
    meta: { title: 'Incident Queue' },
  },
  {
    path: '/history',
    name: 'history',
    component: History,
    meta: { title: 'Incident History' },
  },
  {
    path: '/cameras',
    name: 'cameras',
    component: Cameras,
    meta: { title: 'Cameras' },
  },
];

export const router = createRouter({
  history: createWebHistory(),
  routes,
});

router.afterEach((to) => {
  const title = (to.meta?.title as string) ?? 'Operator Console';
  document.title = `VeilWatch — ${title}`;
});
