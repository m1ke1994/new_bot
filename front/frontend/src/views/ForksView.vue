<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

const state = ref({
  status: 'IDLE',
  authorized: false,
  auth_status: 'UNKNOWN',
  browser: { status: 'CLOSED' },
  scanning: false,
  leagues_found: 0,
  matches_found: 0,
  candidates_count: 0,
  active_match_id: null,
  active_match: null,
  current_league: null,
  last_scan_finished_at: null,
  updated_at: null,
  league_errors: [],
})
const leagues = ref([])
const matches = ref([])
const candidates = ref([])
const selectedLeague = ref('ALL')
const liveMatchesOpen = ref(false)
const pending = ref(null)
const errorMessage = ref('')
let pagePoll = null

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

async function loadState() {
  state.value = await api('/api/table-tennis/state')
}

async function loadResults() {
  const [leagueData, matchData, candidateData] = await Promise.all([
    api('/api/table-tennis/leagues'),
    api('/api/table-tennis/matches'),
    api('/api/table-tennis/candidates'),
  ])
  leagues.value = Array.isArray(leagueData.items) ? leagueData.items : []
  matches.value = Array.isArray(matchData.items) ? matchData.items : []
  candidates.value = Array.isArray(candidateData.items) ? candidateData.items : []
}

async function loadAll() {
  try {
    await Promise.all([loadState(), loadResults()])
    errorMessage.value = ''
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : String(error)
  }
}

async function scan() {
  if (pending.value || state.value.scanning) return
  pending.value = 'scan'
  errorMessage.value = ''
  state.value = { ...state.value, scanning: true, status: 'STARTING' }
  try {
    state.value = await api('/api/table-tennis/scan', { method: 'POST' })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : String(error)
  } finally {
    pending.value = null
    await loadAll()
  }
}

async function startForks() {
  if (pending.value || state.value.scanning) return
  pending.value = 'start'
  errorMessage.value = ''
  try {
    state.value = await api('/api/table-tennis/start', { method: 'POST' })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : String(error)
  } finally {
    pending.value = null
    await loadAll()
  }
}

async function stopForks() {
  if (pending.value) return
  pending.value = 'stop'
  errorMessage.value = ''
  try {
    state.value = await api('/api/table-tennis/stop', { method: 'POST' })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : String(error)
  } finally {
    pending.value = null
    await loadAll()
  }
}

function show(value, fallback = '—') {
  return value === null || value === undefined || value === '' ? fallback : value
}

function formatDate(value) {
  if (!value) return '—'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleTimeString('ru-RU')
}

function formatOdd(value) {
  const number = Number(value)
  return Number.isFinite(number) ? number.toFixed(3).replace(/0+$/, '').replace(/\.$/, '') : '—'
}

function matchKey(item) {
  return item.event_id || `${item.league_id}-${item.player_1}-${item.player_2}-${item.href || ''}`
}

function isActive(item) {
  return Boolean(item?.event_id && state.value.active_match_id === item.event_id)
}

function oddsTrend(current, initial) {
  const currentNumber = Number(current)
  const initialNumber = Number(initial)
  if (!Number.isFinite(currentNumber) || !Number.isFinite(initialNumber) || currentNumber === initialNumber) return ''
  return currentNumber > initialNumber ? '↑' : '↓'
}

const filteredMatches = computed(() => (
  selectedLeague.value === 'ALL'
    ? matches.value
    : matches.value.filter((item) => item.league_id === selectedLeague.value)
))

const candidateRows = computed(() => {
  const rows = [...candidates.value]
  const active = state.value.active_match
  if (!active?.event_id) return rows
  const index = rows.findIndex((item) => item.event_id === active.event_id)
  if (index >= 0) rows[index] = active
  else rows.unshift(active)
  return rows
})

const scannerStatus = computed(() => {
  if (state.value.active_match_id) return 'МОНИТОРИНГ'
  if (state.value.scanning) return state.value.status || 'СКАНИРОВАНИЕ'
  return state.value.status || 'IDLE'
})

onMounted(async () => {
  await loadAll()
  pagePoll = window.setInterval(async () => {
    try {
      await Promise.all([loadState(), loadResults()])
    } catch (_error) {
      // Keep the last visible snapshot; explicit actions show errors.
    }
  }, 1500)
})

onBeforeUnmount(() => {
  if (pagePoll) window.clearInterval(pagePoll)
})
</script>

<template>
  <main class="forks-page">
    <header class="forks-hero">
      <div>
        <span class="forks-eyebrow">READ-ONLY · LIVE ODDS MONITOR</span>
        <h1>Вилки — настольный теннис</h1>
        <p>Отбор матчей в 1-й/2-й партии, переход на следующую партию и мониторинг П1 / П2 без размещения ставок.</p>
      </div>
      <div class="forks-actions">
        <RouterLink class="forks-button secondary" to="/">Вернуться к стратегиям</RouterLink>
        <button class="forks-button" :disabled="pending || state.scanning" @click="startForks">
          {{ pending === 'start' ? 'Запуск...' : 'Запустить DEMO вилки' }}
        </button>
        <button class="forks-button secondary" :disabled="pending || (!state.scanning && state.browser?.status !== 'OPEN')" @click="stopForks">
          {{ pending === 'stop' ? 'Остановка...' : 'Остановить' }}
        </button>
        <button class="forks-button secondary" :disabled="pending || state.scanning" @click="scan">
          {{ pending === 'scan' || state.scanning ? 'Сканирование...' : 'Обновить матчи' }}
        </button>
      </div>
    </header>

    <section v-if="errorMessage" class="forks-alert">{{ errorMessage }}</section>

    <section class="forks-status-grid">
      <article><span>Авторизация</span><strong>{{ state.authorized ? 'Да' : show(state.auth_status, 'Нет') }}</strong></article>
      <article><span>Браузер</span><strong>{{ state.browser?.status === 'OPEN' ? 'Запущен' : 'Остановлен' }}</strong></article>
      <article><span>Статус</span><strong>{{ scannerStatus }}</strong></article>
      <article><span>Кандидатов</span><strong>{{ state.candidates_count || candidateRows.length }}</strong></article>
      <article><span>Матчей</span><strong>{{ state.matches_found || matches.length }}</strong></article>
      <article><span>Последнее обновление</span><strong>{{ formatDate(state.last_scan_finished_at || state.updated_at) }}</strong></article>
    </section>

    <section v-if="state.current_league" class="forks-progress">
      Сейчас сканируется: <strong>{{ state.current_league }}</strong>
    </section>

    <section v-if="state.league_errors?.length" class="forks-errors">
      <h2>Ошибки отдельных лиг</h2>
      <p v-for="item in state.league_errors" :key="`${item.league}-${item.error}`">
        <strong>{{ item.league }}</strong>: {{ item.error }}
      </p>
    </section>

    <section class="forks-panel monitor-panel">
      <div class="forks-section-title">
        <div><span>FORKS MONITOR</span><h2>Вилки — мониторинг</h2></div>
        <b>{{ candidateRows.length }}</b>
      </div>

      <div v-if="state.active_match" class="active-observation">
        <div class="active-observation-heading">
          <div>
            <span class="monitor-badge">● МОНИТОРИНГ</span>
            <strong>{{ state.active_match.player_1 }} — {{ state.active_match.player_2 }}</strong>
            <small>{{ show(state.active_match.league_name) }}</small>
          </div>
          <a v-if="state.active_match.url" :href="state.active_match.url" target="_blank" rel="noopener noreferrer">Открыть матч</a>
        </div>
        <div class="active-observation-grid">
          <div><span>Текущая партия</span><strong>{{ show(state.active_match.current_set) }}</strong></div>
          <div><span>Следующая партия</span><strong>{{ show(state.active_match.target_set) }}</strong></div>
          <div><span>Начальный П1</span><strong>{{ formatOdd(state.active_match.initial_odds_p1) }}</strong></div>
          <div><span>Начальный П2</span><strong>{{ formatOdd(state.active_match.initial_odds_p2) }}</strong></div>
          <div><span>Текущий П1</span><strong>{{ formatOdd(state.active_match.current_odds_p1) }} {{ oddsTrend(state.active_match.current_odds_p1, state.active_match.initial_odds_p1) }}</strong></div>
          <div><span>Текущий П2</span><strong>{{ formatOdd(state.active_match.current_odds_p2) }} {{ oddsTrend(state.active_match.current_odds_p2, state.active_match.initial_odds_p2) }}</strong></div>
          <div><span>Рынок</span><strong>{{ show(state.active_match.market_status) }}</strong></div>
          <div><span>Статус</span><strong>{{ show(state.active_match.monitoring_status) }}</strong></div>
        </div>
      </div>

      <div class="forks-table-wrap">
        <table class="candidates-table">
          <thead>
            <tr>
              <th>Лига</th><th>Матч</th><th>Текущая партия</th><th>Следующая партия</th>
              <th>Начальный П1</th><th>Начальный П2</th><th>Текущий П1</th><th>Текущий П2</th>
              <th>Рынок</th><th>Статус</th><th>Ссылка</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in candidateRows" :key="matchKey(item)" :class="{ 'active-row': isActive(item) }">
              <td>{{ show(item.league_name) }}</td>
              <td><strong>{{ item.player_1 }} — {{ item.player_2 }}</strong><span v-if="isActive(item)" class="inline-monitor">● мониторинг</span></td>
              <td>{{ show(item.current_set) }}</td>
              <td>{{ show(item.target_set) }}</td>
              <td>{{ formatOdd(item.initial_odds_p1) }}</td>
              <td>{{ formatOdd(item.initial_odds_p2) }}</td>
              <td>{{ formatOdd(item.current_odds_p1) }} <span class="trend">{{ oddsTrend(item.current_odds_p1, item.initial_odds_p1) }}</span></td>
              <td>{{ formatOdd(item.current_odds_p2) }} <span class="trend">{{ oddsTrend(item.current_odds_p2, item.initial_odds_p2) }}</span></td>
              <td>{{ show(item.market_status) }}</td>
              <td>{{ show(item.monitoring_status) }}</td>
              <td><a v-if="item.url" :href="item.url" target="_blank" rel="noopener noreferrer">открыть</a><span v-else>—</span></td>
            </tr>
            <tr v-if="!candidateRows.length"><td colspan="11" class="forks-empty">Кандидаты появятся, когда найдутся LIVE-матчи в 1-й или 2-й партии</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="forks-panel live-matches-panel">
      <button class="live-matches-toggle" type="button" @click="liveMatchesOpen = !liveMatchesOpen">
        <span>{{ liveMatchesOpen ? '▼' : '▶' }}</span>
        <div><small>LIVE MATCHES</small><strong>Матчи ({{ matches.length }})</strong></div>
        <span class="toggle-hint">{{ liveMatchesOpen ? 'Скрыть' : 'Показать' }}</span>
      </button>

      <div v-if="liveMatchesOpen" class="live-matches-content">
        <div class="live-matches-filter">
          <label>
            Лига
            <select v-model="selectedLeague">
              <option value="ALL">Все лиги</option>
              <option v-for="league in leagues" :key="league.league_id" :value="league.league_id">{{ league.name }}</option>
            </select>
          </label>
        </div>
        <div class="forks-table-wrap">
          <table>
            <thead><tr><th>Event ID</th><th>Лига</th><th>Игрок 1</th><th>Игрок 2</th><th>Партия</th><th>Счёт</th><th>Кф П1</th><th>Кф П2</th><th>Статус</th><th>Время</th><th>Ссылка</th></tr></thead>
            <tbody>
              <tr v-for="item in filteredMatches" :key="matchKey(item)">
                <td>{{ show(item.event_id) }}</td><td>{{ show(item.league_name) }}</td><td>{{ item.player_1 }}</td><td>{{ item.player_2 }}</td>
                <td>{{ show(item.current_set) }}</td><td>{{ show(item.score) }}</td><td>{{ formatOdd(item.odds?.p1) }}</td><td>{{ formatOdd(item.odds?.p2) }}</td>
                <td>{{ show(item.status) }}</td><td>{{ show(item.time || item.period) }}</td>
                <td><a v-if="item.url" :href="item.url" target="_blank" rel="noopener noreferrer">открыть</a><span v-else>—</span></td>
              </tr>
              <tr v-if="!filteredMatches.length"><td colspan="11" class="forks-empty">Матчи пока не собраны</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>
  </main>
</template>

<style scoped>
.forks-page { min-height: 100vh; padding: 28px; color: #e7edf6; background: #080c12; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
.forks-hero, .forks-section-title, .active-observation-heading { display: flex; justify-content: space-between; align-items: center; gap: 24px; }
.forks-hero { padding: 26px; border: 1px solid #273244; border-radius: 12px; background: linear-gradient(135deg, #111923, #0c121b); }
.forks-eyebrow, .forks-section-title span { color: #38d995; font-size: 11px; font-weight: 800; letter-spacing: .12em; }
h1 { margin: 7px 0; font-size: clamp(28px, 4vw, 46px); } h2 { margin: 4px 0 0; font-size: 19px; }
.forks-hero p { margin: 0; max-width: 760px; color: #97a5b7; }
.forks-actions { display: flex; gap: 10px; flex-wrap: wrap; }
.forks-button { min-height: 42px; padding: 0 17px; border: 1px solid rgba(56,217,149,.45); border-radius: 8px; color: #38d995; background: rgba(56,217,149,.09); cursor: pointer; font-weight: 750; text-decoration: none; display: inline-flex; align-items: center; }
.forks-button.secondary { color: #b7c2d1; border-color: #354359; background: #111923; }.forks-button:disabled { opacity: .45; cursor: not-allowed; }
.forks-status-grid { display: grid; grid-template-columns: repeat(6, minmax(0,1fr)); gap: 10px; margin-top: 14px; }
.forks-status-grid article { padding: 15px; border: 1px solid #202b3a; border-radius: 9px; background: #0e151f; }
.forks-status-grid span, th, .active-observation-grid span { color: #718096; font-size: 10px; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }.forks-status-grid strong { display: block; margin-top: 7px; }
.forks-panel, .forks-errors, .forks-alert, .forks-progress { margin-top: 14px; border: 1px solid #202b3a; border-radius: 10px; background: #0e151f; overflow: hidden; }
.forks-section-title { padding: 17px 19px; border-bottom: 1px solid #202b3a; }.forks-section-title b { color: #38d995; }
.active-observation { margin: 14px; padding: 17px; border: 1px solid rgba(56,217,149,.32); border-radius: 9px; background: rgba(56,217,149,.05); }
.active-observation-heading > div { display: grid; gap: 4px; }.active-observation-heading strong { font-size: 17px; }.active-observation-heading small { color: #8d9aad; }.active-observation-heading a { color: #38d995; }
.monitor-badge, .inline-monitor { color: #38d995; font-size: 10px; font-weight: 800; text-transform: uppercase; letter-spacing: .08em; }.inline-monitor { margin-left: 9px; }
.active-observation-grid { display: grid; grid-template-columns: repeat(8, minmax(0,1fr)); gap: 8px; margin-top: 15px; }.active-observation-grid > div { padding: 11px; border-radius: 7px; background: #0b1119; }.active-observation-grid strong { display: block; margin-top: 5px; font-size: 13px; }
.forks-table-wrap { overflow: auto; } table { width: 100%; border-collapse: collapse; min-width: 1050px; } th, td { padding: 12px 14px; border-bottom: 1px solid #1b2532; text-align: left; white-space: nowrap; } td { font-size: 12px; } a { color: #38d995; }
.active-row { background: rgba(56,217,149,.07); }.trend { color: #38d995; font-weight: 800; }
.forks-empty { padding: 28px; color: #718096; text-align: center; }.forks-alert { padding: 14px; color: #ff8994; border-color: rgba(255,104,118,.4); }.forks-progress { padding: 13px 16px; color: #aeb9c8; }.forks-progress strong { color: #38d995; }
.forks-errors { padding: 16px 19px; border-color: rgba(255,180,76,.35); }.forks-errors h2 { color: #ffb44c; }.forks-errors p { margin: 8px 0 0; color: #aeb9c8; font-size: 12px; }
.live-matches-toggle { width: 100%; padding: 17px 19px; border: 0; color: #e7edf6; background: transparent; display: flex; align-items: center; gap: 12px; text-align: left; cursor: pointer; }.live-matches-toggle div { display: grid; gap: 3px; }.live-matches-toggle small { color: #38d995; font-weight: 800; letter-spacing: .1em; }.live-matches-toggle strong { font-size: 18px; }.toggle-hint { margin-left: auto; color: #718096; font-size: 12px; }
.live-matches-content { border-top: 1px solid #202b3a; }.live-matches-filter { padding: 12px 18px; border-bottom: 1px solid #202b3a; }.live-matches-filter label { display: flex; align-items: center; gap: 9px; color: #8d9aad; font-size: 12px; }.live-matches-filter select { min-height: 34px; max-width: 320px; padding: 0 9px; border: 1px solid #354359; border-radius: 7px; color: #e7edf6; background: #111923; }
@media (max-width: 1200px) { .active-observation-grid { grid-template-columns: repeat(4, 1fr); }.forks-status-grid { grid-template-columns: repeat(3, 1fr); } }
@media (max-width: 680px) { .forks-page { padding: 14px; }.forks-hero, .active-observation-heading { align-items: flex-start; flex-direction: column; }.forks-status-grid, .active-observation-grid { grid-template-columns: repeat(2, 1fr); }.forks-actions { width: 100%; }.forks-button { flex: 1; justify-content: center; } }
</style>
