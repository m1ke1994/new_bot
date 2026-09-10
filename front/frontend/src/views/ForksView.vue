<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

const state = ref({ status: 'IDLE', scanning: false, browser: { status: 'CLOSED' } })
const matches = ref([])
const candidates = ref([])
const forks = ref([])
const budget = ref(10000)
const initialStake = ref(1000)
const pending = ref('')
const errorMessage = ref('')
let poll = null

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = payload.detail
    throw new Error(typeof detail === 'string' ? detail : detail?.message || `HTTP ${response.status}`)
  }
  return payload
}

async function loadAll() {
  try {
    const [stateData, matchData, candidateData, forkData] = await Promise.all([
      api('/api/table-tennis/state'),
      api('/api/table-tennis/matches'),
      api('/api/table-tennis/candidates'),
      api('/api/table-tennis/forks'),
    ])
    state.value = stateData
    matches.value = Array.isArray(matchData.items) ? matchData.items : []
    candidates.value = Array.isArray(candidateData.items) ? candidateData.items : []
    forks.value = Array.isArray(forkData.items) ? forkData.items : []
    errorMessage.value = ''
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : String(error)
  }
}

async function startForks() {
  if (pending.value || state.value.scanning) return
  pending.value = 'start'
  try {
    await api('/api/table-tennis/start', {
      method: 'POST',
      body: JSON.stringify({
        budget: Number(budget.value),
        initial_stake: Number(initialStake.value),
      }),
    })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : String(error)
  } finally {
    pending.value = ''
    await loadAll()
  }
}

async function stopForks() {
  if (pending.value) return
  pending.value = 'stop'
  try {
    await api('/api/table-tennis/stop', { method: 'POST' })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : String(error)
  } finally {
    pending.value = ''
    await loadAll()
  }
}

async function clearForks() {
  if (pending.value) return
  pending.value = 'clear'
  try {
    await api('/api/table-tennis/forks', { method: 'DELETE' })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : String(error)
  } finally {
    pending.value = ''
    await loadAll()
  }
}

function show(value, fallback = '—') {
  return value === null || value === undefined || value === '' ? fallback : value
}

function odd(value) {
  const number = Number(value)
  return Number.isFinite(number) ? number.toFixed(3).replace(/0+$/, '').replace(/\.$/, '') : '—'
}

function money(value) {
  const number = Number(value)
  return Number.isFinite(number) ? `${number.toFixed(2)} ₽` : '—'
}

function percent(value) {
  const number = Number(value)
  return Number.isFinite(number) ? `${number.toFixed(2)}%` : '—'
}

const active = computed(() => state.value.active_match || null)
const queue = computed(() => candidates.value.filter((item) => item.event_id !== active.value?.event_id))
const canStart = computed(() => (
  !pending.value
  && !state.value.scanning
  && Number(budget.value) > 0
  && Number(initialStake.value) > 0
  && Number(initialStake.value) <= Number(budget.value)
))

onMounted(async () => {
  await loadAll()
  poll = window.setInterval(loadAll, 1000)
})

onBeforeUnmount(() => {
  if (poll) window.clearInterval(poll)
})
</script>

<template>
  <main class="forks-page">
    <header class="hero">
      <div>
        <span class="eyebrow">PAPER TRADING · TABLE TENNIS</span>
        <h1>Вилки — настольный теннис</h1>
        <p>Сканирование всех LIVE-лиг, отбор матчей со счётом 0:0, мониторинг 1-й партии и расчёт двух плеч вилки без записи в БД.</p>
      </div>
      <RouterLink class="btn secondary" to="/">К стратегиям</RouterLink>
    </header>

    <section v-if="errorMessage" class="alert">{{ errorMessage }}</section>

    <section class="panel settings-panel">
      <div>
        <span class="section-label">НАСТРОЙКА ПЕРЕД ЗАПУСКОМ</span>
        <h2>Бюджет и первая ставка</h2>
      </div>
      <div class="settings-grid">
        <label>
          Общий бюджет, ₽
          <input v-model.number="budget" type="number" min="1" step="1" :disabled="state.scanning" />
        </label>
        <label>
          Первоначальная ставка, ₽
          <input v-model.number="initialStake" type="number" min="1" step="1" :disabled="state.scanning" />
        </label>
      </div>
      <div class="actions">
        <button class="btn" :disabled="!canStart" @click="startForks">
          {{ pending === 'start' ? 'Запуск...' : 'Запустить вилки' }}
        </button>
        <button class="btn secondary" :disabled="pending || !state.scanning" @click="stopForks">
          {{ pending === 'stop' ? 'Остановка...' : 'Остановить' }}
        </button>
        <button class="btn danger" :disabled="pending || state.scanning" @click="clearForks">
          {{ pending === 'clear' ? 'Очистка...' : 'Очистить историю' }}
        </button>
      </div>
    </section>

    <section class="stats-grid">
      <article><span>Статус</span><strong>{{ show(state.status) }}</strong></article>
      <article><span>Авторизация</span><strong>{{ state.authorized ? 'Да' : show(state.auth_status, 'Нет') }}</strong></article>
      <article><span>Матчей 0:0</span><strong>{{ state.matches_found || matches.length }}</strong></article>
      <article><span>Вилок</span><strong>{{ state.forks_count || forks.length }}</strong></article>
      <article><span>Бюджет</span><strong>{{ money(state.fork_budget) }}</strong></article>
      <article><span>Свободно</span><strong>{{ money(state.fork_available_budget) }}</strong></article>
    </section>

    <section class="panel" v-if="active">
      <div class="section-heading">
        <div><span class="section-label">АКТИВНЫЙ МАТЧ</span><h2>{{ active.player_1 }} — {{ active.player_2 }}</h2></div>
        <a v-if="active.url" :href="active.url" target="_blank" rel="noopener noreferrer">Открыть матч</a>
      </div>

      <div class="active-grid">
        <div><span>Лига</span><strong>{{ show(active.league_name) }}</strong></div>
        <div><span>Счёт при отборе</span><strong>{{ show(active.score) }}</strong></div>
        <div><span>П1 сейчас</span><strong>{{ odd(active.current_odds_p1) }}</strong></div>
        <div><span>П2 сейчас</span><strong>{{ odd(active.current_odds_p2) }}</strong></div>
        <div><span>Первое плечо</span><strong>{{ active.first_leg ? `${active.first_leg.player} · ${odd(active.first_leg.odds)} · ${money(active.first_leg.stake)}` : 'ожидание' }}</strong></div>
        <div><span>Минимум второго кф</span><strong>{{ odd(active.minimum_second_odds) }}</strong></div>
        <div><span>Расчёт второй суммы</span><strong>{{ money(active.required_second_stake) }}</strong></div>
        <div><span>Текущий % вилки</span><strong>{{ percent(active.fork_percent_preview) }}</strong></div>
      </div>
      <div class="status-strip">{{ show(active.monitoring_status) }}</div>
    </section>

    <section class="panel">
      <div class="section-heading">
        <div><span class="section-label">ОЧЕРЕДЬ</span><h2>Матчи со счётом 0:0</h2></div>
        <b>{{ queue.length }}</b>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Лига</th><th>Матч</th><th>Счёт</th><th>Время</th><th>П1</th><th>П2</th><th>Ссылка</th></tr></thead>
          <tbody>
            <tr v-for="item in queue" :key="item.event_id || item.url">
              <td>{{ show(item.league_name) }}</td>
              <td><strong>{{ item.player_1 }} — {{ item.player_2 }}</strong></td>
              <td>{{ show(item.score) }}</td>
              <td>{{ show(item.time || item.period) }}</td>
              <td>{{ odd(item.odds?.p1) }}</td>
              <td>{{ odd(item.odds?.p2) }}</td>
              <td><a v-if="item.url" :href="item.url" target="_blank" rel="noopener noreferrer">открыть</a></td>
            </tr>
            <tr v-if="!queue.length"><td colspan="7" class="empty">Подходящих матчей пока нет</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="panel">
      <div class="section-heading">
        <div><span class="section-label">ИСТОРИЯ В ПАМЯТИ</span><h2>Зафиксированные вилки</h2></div>
        <b>{{ forks.length }}</b>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Матч</th><th>1-е плечо</th><th>2-е плечо</th><th>Всего</th><th>Гарант. плюс</th><th>Вилка</th></tr></thead>
          <tbody>
            <tr v-for="item in forks" :key="`${item.event_id}-${item.completed_at}`">
              <td><strong>{{ item.player_1 }} — {{ item.player_2 }}</strong><small>{{ show(item.league_name) }}</small></td>
              <td>{{ item.first_leg.player }} · {{ odd(item.first_leg.odds) }} · {{ money(item.first_leg.stake) }}</td>
              <td>{{ item.second_leg.player }} · {{ odd(item.second_leg.odds) }} · {{ money(item.second_leg.stake) }}</td>
              <td>{{ money(item.fork.total_stake) }}</td>
              <td>{{ money(item.fork.guaranteed_profit) }}</td>
              <td><strong>{{ percent(item.fork.fork_percent) }}</strong></td>
            </tr>
            <tr v-if="!forks.length"><td colspan="6" class="empty">Завершённых вилок пока нет</td></tr>
          </tbody>
        </table>
      </div>
    </section>
  </main>
</template>

<style scoped>
.forks-page { min-height: 100vh; padding: 28px; background: #081019; color: #e9eef6; }
.hero, .section-heading, .actions { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.hero { margin-bottom: 20px; }
h1 { margin: 6px 0; font-size: 34px; }
h2 { margin: 4px 0 0; }
p { color: #9ba9bb; max-width: 820px; }
.eyebrow, .section-label { font-size: 12px; letter-spacing: .12em; color: #7f8da0; }
.panel { background: #0e1824; border: 1px solid #1e2a39; border-radius: 16px; padding: 20px; margin-bottom: 18px; }
.settings-grid, .stats-grid, .active-grid { display: grid; gap: 12px; }
.settings-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); margin: 18px 0; }
.stats-grid { grid-template-columns: repeat(6, minmax(0, 1fr)); margin-bottom: 18px; }
.active-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); margin-top: 18px; }
.stats-grid article, .active-grid > div { background: #111f2d; border: 1px solid #223246; border-radius: 12px; padding: 14px; }
.stats-grid span, .active-grid span { display: block; color: #8f9db0; font-size: 12px; margin-bottom: 6px; }
label { display: grid; gap: 8px; color: #9daabd; }
input { background: #08121c; color: #fff; border: 1px solid #2a3a4e; border-radius: 10px; padding: 12px; font-size: 16px; }
.btn { border: 0; border-radius: 10px; padding: 11px 16px; cursor: pointer; background: #fff; color: #0b1118; font-weight: 700; text-decoration: none; }
.btn.secondary { background: #182534; color: #dce5f1; }
.btn.danger { background: #392026; color: #ffd9de; }
.btn:disabled { opacity: .45; cursor: not-allowed; }
.alert { background: #3b2025; border: 1px solid #6b313a; padding: 12px 16px; border-radius: 10px; margin-bottom: 16px; }
.status-strip { margin-top: 14px; padding: 10px 12px; border-radius: 8px; background: #0a141f; color: #cbd7e6; }
.table-wrap { overflow-x: auto; margin-top: 14px; }
table { width: 100%; border-collapse: collapse; min-width: 860px; }
th, td { padding: 12px 10px; border-bottom: 1px solid #1d2a39; text-align: left; font-size: 13px; }
th { color: #7f8da0; font-weight: 600; }
td small { display: block; color: #7f8da0; margin-top: 4px; }
a { color: #c7ddff; }
.empty { text-align: center; color: #738195; padding: 26px; }
@media (max-width: 1100px) { .stats-grid { grid-template-columns: repeat(3, 1fr); } .active-grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 700px) { .forks-page { padding: 16px; } .hero, .section-heading, .actions { align-items: stretch; flex-direction: column; } .settings-grid, .stats-grid, .active-grid { grid-template-columns: 1fr; } }
</style>
