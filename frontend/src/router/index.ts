import { createRouter, createWebHistory } from 'vue-router'
import Workspace from '../views/Workspace.vue'

export const router = createRouter({ history: createWebHistory(), routes: [
  { path: '/', redirect: '/overview' },
  { path: '/:section(overview|knowledge|chat|retrieval|tools|evaluation|monitoring)', component: Workspace },
  { path: '/:pathMatch(.*)*', redirect: '/overview' },
] })
