import { createRouter, createWebHistory } from 'vue-router'

import DashboardView from '../App.vue'
import ForksView from '../views/ForksView.vue'


export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'dashboard', component: DashboardView },
    { path: '/forks', name: 'forks', component: ForksView },
  ],
})
