<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

const state = ref({
  status: 'IDLE', authorized: false, auth_status: 'UNKNOWN',
  browser: { status: 'CLOSED' }, scanning: false,
  leagues_found: 0, matches_found: 0, current_league: null,
  last_scan_finished_at: null, updated_at: null, league_errors: [],
})
const leagues = ref([])
const matches = ref([])
const selectedLeague = ref('ALL')
const pending = ref(null)
const errorMessage = ref('')
let pagePoll = null

async function api(path, options = {}) {
  const response = await fetch(path, {
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
  const [leagueData, matchData] = await Promise.all([
    api('/api/table-tennis/leagues'),
    api('/api/table-tennis/matches'),
  ])
  leagues.value = Array.isArray(leagueData.items) ? leagueData.items : []
  matches.value = Array.isArray(matchData.items) ? matchData.items : []
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

function formatMarkets(markets) {
  if (!markets || typeof markets !== 'object') return '—'
  const values = Object.entries(markets).map(([name, value]) => `${name}: ${value}`)
  return values.length ? values.join(' · ') : '—'
}

const filteredMatches = computed(() => (
  selectedLeague.value === 'ALL'
    ? matches.value
    : matches.value.filter((item) => item.league_id === selectedLeague.value)
))

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
        <span class="forks-eyebrow">READ-ONLY SCANNER · STAGE 1</span>
        <h1>Вилки — настольный теннис</h1>
        <p>Сбор LIVE-лиг, матчей и доступных коэффициентов без анализа и размещения ставок.</p>
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
      <article><span>Сканер</span><strong>{{ state.status }}</strong></article>
      <article><span>Лиг</span><strong>{{ state.leagues_found || 0 }}</strong></article>
      <article><span>Матчей</span><strong>{{ state.matches_found || 0 }}</strong></article>
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

    <section class="forks-panel">
      <div class="forks-section-title"><div><span>LEAGUES</span><h2>Лиги</h2></div><b>{{ leagues.length }}</b></div>
      <div class="forks-table-wrap">
        <table>
          <thead><tr><th>Лига</th><th>Группа</th><th>Матчей на сайте</th><th>Собрано</th><th>URL</th></tr></thead>
          <tbody>
            <tr v-for="league in leagues" :key="league.league_id">
              <td>{{ league.name }}</td><td>{{ show(league.group_name) }}</td>
              <td>{{ show(league.declared_games_count) }}</td><td>{{ league.parsed_games_count || 0 }}</td>
              <td><a :href="league.url" target="_blank" rel="noopener noreferrer">открыть</a></td>
            </tr>
            <tr v-if="!leagues.length"><td colspan="5" class="forks-empty">Запустите сканирование, чтобы получить список лиг</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="forks-panel">
      <div class="forks-section-title">
        <div><span>LIVE MATCHES</span><h2>Матчи</h2></div>
        <select v-model="selectedLeague">
          <option value="ALL">Все лиги</option>
          <option v-for="league in leagues" :key="league.league_id" :value="league.league_id">{{ league.name }}</option>
        </select>
      </div>
      <div class="forks-table-wrap">
        <table>
          <thead><tr><th>Event ID</th><th>Лига</th><th>Игрок 1</th><th>Игрок 2</th><th>Счёт</th><th>Партии / raw</th><th>Текущая партия</th><th>Кф 1</th><th>Кф 2</th><th>Рынки</th><th>Статус</th><th>Время</th><th>Ссылка</th></tr></thead>
          <tbody>
            <tr v-for="item in filteredMatches" :key="item.event_id || `${item.league_id}-${item.player_1}-${item.player_2}-${item.href}`">
              <td>{{ show(item.event_id) }}</td><td>{{ item.league_name }}</td><td>{{ item.player_1 }}</td><td>{{ item.player_2 }}</td>
              <td>{{ show(item.score) }}</td><td>{{ show(item.sets_score || item.raw_score_values?.join(' · ')) }}</td>
              <td>{{ show(item.current_set) }}</td><td>{{ show(item.odds?.p1) }}</td><td>{{ show(item.odds?.p2) }}</td><td>{{ formatMarkets(item.markets) }}</td>
              <td>{{ show(item.status) }}</td><td>{{ show(item.time || item.period) }}</td>
              <td><a v-if="item.url" :href="item.url" target="_blank" rel="noopener noreferrer">открыть</a><span v-else>—</span></td>
            </tr>
            <tr v-if="!filteredMatches.length"><td colspan="13" class="forks-empty">Матчи пока не собраны</td></tr>
          </tbody>
        </table>
      </div>
    </section>
  </main>
</template>

<style scoped>
.forks-page { min-height: 100vh; padding: 28px; color: #e7edf6; background: #080c12; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
.forks-hero, .forks-section-title { display: flex; justify-content: space-between; align-items: center; gap: 24px; }
.forks-hero { padding: 26px; border: 1px solid #273244; border-radius: 12px; background: linear-gradient(135deg, #111923, #0c121b); }
.forks-eyebrow, .forks-section-title span { color: #38d995; font-size: 11px; font-weight: 800; letter-spacing: .12em; }
h1 { margin: 7px 0; font-size: clamp(28px, 4vw, 46px); } h2 { margin: 4px 0 0; font-size: 19px; }
.forks-hero p { margin: 0; color: #97a5b7; }
.forks-actions { display: flex; gap: 10px; flex-wrap: wrap; }
.forks-button { min-height: 42px; padding: 0 17px; border: 1px solid rgba(56,217,149,.45); border-radius: 8px; color: #38d995; background: rgba(56,217,149,.09); cursor: pointer; font-weight: 750; text-decoration: none; display: inline-flex; align-items: center; }
.forks-button.secondary { color: #b7c2d1; border-color: #354359; background: #111923; }.forks-button:disabled { opacity: .45; cursor: not-allowed; }
.forks-status-grid { display: grid; grid-template-columns: repeat(6, minmax(0,1fr)); gap: 10px; margin-top: 14px; }
.forks-status-grid article { padding: 15px; border: 1px solid #202b3a; border-radius: 9px; background: #0e151f; }
.forks-status-grid span, th { color: #718096; font-size: 10px; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }.forks-status-grid strong { display: block; margin-top: 7px; }
.forks-panel, .forks-errors, .forks-alert, .forks-progress { margin-top: 14px; border: 1px solid #202b3a; border-radius: 10px; background: #0e151f; }
.forks-section-title { padding: 17px 19px; border-bottom: 1px solid #202b3a; }.forks-section-title b { color: #38d995; }
.forks-section-title select { min-height: 36px; max-width: 320px; padding: 0 10px; border: 1px solid #354359; border-radius: 7px; color: #e7edf6; background: #111923; }
.forks-table-wrap { overflow: auto; } table { width: 100%; border-collapse: collapse; min-width: 900px; } th, td { padding: 12px 14px; border-bottom: 1px solid #1b2532; text-align: left; white-space: nowrap; } td { font-size: 12px; } a { color: #38d995; }
.forks-empty { padding: 28px; color: #718096; text-align: center; }.forks-alert { padding: 14px; color: #ff8994; border-color: rgba(255,104,118,.4); }.forks-progress { padding: 13px 16px; color: #aeb9c8; }.forks-progress strong { color: #38d995; }
.forks-errors { padding: 16px 19px; border-color: rgba(255,180,76,.35); }.forks-errors h2 { color: #ffb44c; }.forks-errors p { margin: 8px 0 0; color: #aeb9c8; font-size: 12px; }
@media (max-width: 1050px) { .forks-status-grid { grid-template-columns: repeat(3, 1fr); } }
@media (max-width: 680px) { .forks-page { padding: 14px; }.forks-hero { align-items: flex-start; flex-direction: column; }.forks-status-grid { grid-template-columns: repeat(2, 1fr); }.forks-actions { width: 100%; }.forks-button { flex: 1; justify-content: center; } }
</style>
