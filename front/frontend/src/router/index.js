import { createRouter, createWebHistory } from 'vue-router'

import DashboardView from '../App.vue'


export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'dashboard', component: DashboardView },
  ],
})
