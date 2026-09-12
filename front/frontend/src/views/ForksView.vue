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
const liveMatchesOpen = ref(true)
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
  if (value === null || value === undefined || value === '') return '—'
  const number = Number(value)
  return Number.isFinite(number) ? number.toFixed(3).replace(/0+$/, '').replace(/\.$/, '') : '—'
}

function money(value) {
  if (value === null || value === undefined || value === '') return '—'
  const number = Number(value)
  return Number.isFinite(number) ? `${number.toFixed(2)} ₽` : '—'
}

function signedMoney(value) {
  if (value === null || value === undefined || value === '') return '—'
  const number = Number(value)
  if (!Number.isFinite(number)) return '—'
  return `${number >= 0 ? '+' : ''}${number.toFixed(2)} ₽`
}

function percent(value) {
  if (value === null || value === undefined || value === '') return '—'
  const number = Number(value)
  return Number.isFinite(number) ? `${number.toFixed(2)}%` : '—'
}

const active = computed(() => state.value.active_match || null)
const queue = computed(() => candidates.value.filter((item) => item.event_id !== active.value?.event_id))
const statusLabels = {
  SEARCHING_MATCH: 'Ищем подходящий матч',
  OPENING_MATCH: 'Открываем текущий матч',
  SECOND_SET_SELECTED: 'Партия 2 выбрана',
  WAITING_SECOND_SET_MARKET: 'Ожидаем рынок «1X2. 2-я Партия»',
  WAITING_ODDS_DIVERGENCE: 'Ожидаем расхождение коэффициентов',
  FIRST_BET_PLACED: 'Первое плечо зафиксировано',
  WAITING_FOR_ARB: 'Ожидаем коэффициент противоположного игрока',
  ARB_FOUND: 'Арбитраж найден',
  SECOND_BET_PLACED: 'Второе плечо зафиксировано',
  ARB_LOCKED: 'Вилка зафиксирована',
  WAITING_RESULT: 'Ожидаем результат второй партии',
  HEDGE_NOT_FOUND: 'Партия завершилась без второго плеча',
  FINISHED: 'Серия рассчитана',
  MARKET_LOCKED: 'Рынок временно заблокирован',
  INSUFFICIENT_BUDGET: 'Недостаточно бюджета для первого плеча',
  INSUFFICIENT_BUDGET_SECOND_LEG: 'Недостаточно свободного бюджета для второго плеча',
  ERROR: 'Ошибка стратегии',
}
const activeStatus = computed(() => {
  const value = active.value?.monitoring_status || state.value.status
  return statusLabels[value] || show(value)
})
const hedgePlayerName = computed(() => {
  if (!active.value?.second_side) return 'противоположного игрока'
  return active.value.second_side === 'p1' ? active.value.player_1 : active.value.player_2
})
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
        <h1>Вилки — мониторинг</h1>
        <p>DEMO-стратегия выбирает «Партию 2», фиксирует фаворита и следит только за коэффициентом противоположного игрока.</p>
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
      <article><span>Статус</span><strong>{{ statusLabels[state.status] || show(state.status) }}</strong></article>
      <article><span>Авторизация</span><strong>{{ state.authorized ? 'Да' : show(state.auth_status, 'Нет') }}</strong></article>
      <article><span>Матчей 0:0</span><strong>{{ state.matches_found || matches.length }}</strong></article>
      <article><span>Вилок</span><strong>{{ state.forks_count || forks.length }}</strong></article>
      <article><span>Начальный бюджет</span><strong>{{ money(state.fork_initial_budget ?? state.fork_budget) }}</strong></article>
      <article><span>Свободный бюджет</span><strong>{{ money(state.fork_available_budget) }}</strong></article>
    </section>

    <section class="panel">
      <div class="section-heading">
        <div><span class="section-label">ДВИЖЕНИЕ СРЕДСТВ</span><h2>DEMO-банк</h2></div>
      </div>
      <div class="funds-grid">
        <div><span>Начальный банк</span><strong>{{ money(state.starting_balance) }}</strong></div>
        <div><span>Текущий банк</span><strong>{{ money(state.current_balance) }}</strong></div>
        <div><span>Свободно</span><strong>{{ money(state.available_balance) }}</strong></div>
        <div><span>В активных ставках</span><strong>{{ money(state.reserved_balance) }}</strong></div>
        <div><span>Результат текущей серии</span><strong>{{ signedMoney(state.current_series_profit) }}</strong></div>
        <div><span>Общая прибыль/убыток</span><strong>{{ signedMoney(state.realized_profit) }}</strong></div>
        <div><span>ROI</span><strong>{{ percent(state.roi_percent) }}</strong></div>
        <div><span>Завершённых серий</span><strong>{{ state.completed_series || 0 }}</strong></div>
        <div><span>Плюсовых</span><strong>{{ state.winning_series || 0 }}</strong></div>
        <div><span>Минусовых</span><strong>{{ state.losing_series || 0 }}</strong></div>
      </div>
    </section>

    <section class="panel" v-if="active">
      <div class="section-heading">
        <div><span class="section-label">АКТИВНЫЙ МАТЧ</span><h2>{{ active.player_1 }} — {{ active.player_2 }}</h2></div>
        <a v-if="active.url" :href="active.url" target="_blank" rel="noopener noreferrer">Открыть матч</a>
      </div>

      <div class="active-grid">
        <div><span>Лига</span><strong>{{ show(active.league_name) }}</strong></div>
        <div><span>Текущий счёт</span><strong>{{ show(active.current_score || active.score) }}</strong></div>
        <div><span>Партия</span><strong>{{ active.party || active.target_set || 2 }}</strong></div>
        <div><span>Начальный П1</span><strong>{{ odd(active.initial_odds_p1) }}</strong></div>
        <div><span>Начальный П2</span><strong>{{ odd(active.initial_odds_p2) }}</strong></div>
        <div><span>Текущий П1</span><strong>{{ odd(active.current_odds_p1) }}</strong></div>
        <div><span>Текущий П2</span><strong>{{ odd(active.current_odds_p2) }}</strong></div>
        <div><span>Первое плечо</span><strong>{{ active.first_leg ? `${active.first_leg.player} / ${active.first_leg.side.toUpperCase()} · ${odd(active.first_leg.accepted_odd ?? active.first_leg.odds)} · ${money(active.first_leg.stake)}` : 'ожидание' }}</strong></div>
        <div><span>Второе плечо</span><strong>{{ active.second_leg ? `${active.second_leg.player} / ${active.second_leg.side.toUpperCase()} · ${odd(active.second_leg.accepted_odd ?? active.second_leg.odds)} · ${money(active.second_leg.stake)}` : 'ожидание' }}</strong></div>
        <div><span>Следим за</span><strong>{{ hedgePlayerName }}</strong></div>
        <div><span>Нулевой коэффициент</span><strong>&gt; {{ odd(active.minimum_second_odds) }}</strong></div>
        <div><span>Текущий коэффициент хеджа</span><strong>{{ odd(active.current_hedge_odd) }}</strong></div>
        <div><span>Расчёт второй суммы</span><strong>{{ money(active.required_second_stake) }}</strong></div>
        <div><span>Задействовано средств</span><strong>{{ money(active.fork?.total_stake ?? state.fork_reserved) }}</strong></div>
        <div><span>Арбитраж</span><strong>{{ percent(active.arbitrage_percent_preview ?? active.fork_percent_preview) }}</strong></div>
        <div><span>При победе первого плеча</span><strong>{{ money(active.fork?.profit_if_first ?? active.profit_if_first_preview) }}</strong></div>
        <div><span>При победе второго плеча</span><strong>{{ money(active.fork?.profit_if_second ?? active.profit_if_second_preview) }}</strong></div>
      </div>
      <p v-if="active.first_bet_wait_reason" class="reason">Причина: {{ active.first_bet_wait_reason }}</p>
      <div class="status-strip">Статус: {{ activeStatus }}</div>
    </section>

    <section class="panel">
      <div class="section-heading">
        <div><span class="section-label">ОЧЕРЕДЬ</span><h2>Матчи со счётом 0:0</h2></div>
        <button class="btn secondary" @click="liveMatchesOpen = !liveMatchesOpen">{{ liveMatchesOpen ? 'Скрыть' : 'Показать' }} · {{ queue.length }}</button>
      </div>
      <div v-show="liveMatchesOpen" class="table-wrap">
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
        <div><span class="section-label">ИСТОРИЯ В ПАМЯТИ</span><h2>Завершённые серии</h2></div>
        <b>{{ forks.length }}</b>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Матч / серия</th><th>Партия</th><th>FIRST LEG</th><th>HEDGE LEG</th><th>Задействовано</th><th>Результаты</th><th>Арбитраж</th><th>Статус</th></tr></thead>
          <tbody>
            <tr v-for="item in forks" :key="item.sequence_id || `${item.event_id}-${item.completed_at}`">
              <td><strong>{{ item.player_1 }} — {{ item.player_2 }}</strong><small>{{ show(item.sequence_id) }}</small></td>
              <td>{{ item.party || 2 }}</td>
              <td>{{ item.first_leg.player }} · {{ odd(item.first_leg.odds) }} · {{ money(item.first_leg.stake) }}</td>
              <td>{{ item.second_leg ? `${item.second_leg.player} · ${odd(item.second_leg.odds)} · ${money(item.second_leg.stake)}` : 'не найдено' }}</td>
              <td>{{ money(item.total_invested ?? item.fork?.total_stake) }}</td>
              <td>{{ show(item.winner) }}<small>P&amp;L: {{ signedMoney(item.profit_loss) }}</small><small>{{ money(item.balance_before) }} → {{ money(item.balance_after) }}</small></td>
              <td><strong>{{ percent(item.arb_percent ?? item.fork?.arbitrage_percent) }}</strong></td>
              <td>{{ item.status || 'CLOSED' }}</td>
            </tr>
            <tr v-if="!forks.length"><td colspan="8" class="empty">Завершённых вилок пока нет</td></tr>
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
.settings-grid, .stats-grid, .active-grid, .funds-grid { display: grid; gap: 12px; }
.settings-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); margin: 18px 0; }
.stats-grid { grid-template-columns: repeat(6, minmax(0, 1fr)); margin-bottom: 18px; }
.active-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); margin-top: 18px; }
.funds-grid { grid-template-columns: repeat(5, minmax(0, 1fr)); margin-top: 18px; }
.stats-grid article, .active-grid > div, .funds-grid > div { background: #111f2d; border: 1px solid #223246; border-radius: 12px; padding: 14px; }
.stats-grid span, .active-grid span, .funds-grid span { display: block; color: #8f9db0; font-size: 12px; margin-bottom: 6px; }
.reason { margin: 14px 0 0; color: #f2c879; }
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
@media (max-width: 1100px) { .stats-grid, .funds-grid { grid-template-columns: repeat(3, 1fr); } .active-grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 700px) { .forks-page { padding: 16px; } .hero, .section-heading, .actions { align-items: stretch; flex-direction: column; } .settings-grid, .stats-grid, .active-grid, .funds-grid { grid-template-columns: 1fr; } }
</style>
