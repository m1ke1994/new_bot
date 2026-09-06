<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

const emptyStats = {
  matches_processed: 0,
  bets: 0,
  wins: 0,
  losses: 0,
  total_amount: 0,
  average_odds: null,
  max_step: 0,
  average_steps_to_win: null,
}

const state = ref({
  running: false,
  mode: 'DEMO',
  status: 'STOPPED',
  browser: { status: 'CLOSED', context: 'CLOSED', page: 'CLOSED' },
  auth: { status: 'UNKNOWN' },
  league: 'FC 25. 3x3. Лига Конференций',
  message: 'Подключение к backend...',
  error: null,
  match: null,
  scanner: { total: 0, started: 0, upcoming: 0, selected: null },
  selected_team: null,
  other_team: null,
  selection_reason: null,
  odds: {
    selected: null,
    opponent: null,
    team1: null,
    team2: null,
    source: null,
    backend: null,
    confidence: null,
    status: 'WAITING',
  },
  ocr: { status: 'NOT_USED_FOR_NEXT_GOAL', attempt: 0, max_attempts: 0, candidates: [] },
  market_reader: { source: 'DOM / Playwright', status: 'WAITING', attempt: 0 },
  bet: { step: 0, max_steps: 7 },
  budget: { initial_budget: 4142, current_budget: 4142, session_profit: 0 },
  last_change: null,
  stats: emptyStats,
  updated_at: null,
})

const history = ref([])
const logs = ref([])
const strategyConfig = ref({ initial_stake: 20, progression_multiplier: 2.2, max_steps: 7, stakes: [20, 44, 97, 213, 469, 1031, 2268], required_budget: 4142 })
const backendError = ref('')
const backendAvailable = ref(false)
const selectedMode = ref('DEMO')
const pendingAction = ref(null)
const actionPending = computed(() => pendingAction.value !== null)
const initialLoading = ref(true)
let timer = null
let tick = 0
let refreshInFlight = false

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  const contentType = response.headers.get('content-type') || ''
  if (!contentType.includes('application/json')) {
    throw new Error(`Backend вернул не JSON (${response.status})`)
  }
  const result = await response.json()
  if (!response.ok) {
    const detail = result.detail
    const message = typeof detail === 'object' && detail !== null
      ? [detail.code, detail.message].filter(Boolean).join(': ')
      : detail
    throw new Error(message || `HTTP ${response.status}`)
  }
  return result
}

async function loadState() {
  try {
    state.value = await api('/api/demo/state')
    if (state.value.running && ['DEMO', 'LIVE'].includes(state.value.mode)) {
      selectedMode.value = state.value.mode
    }
    backendAvailable.value = true
    backendError.value = ''
  } catch (error) {
    backendAvailable.value = false
    const message = error instanceof Error ? error.message : String(error)
    backendError.value = `Backend недоступен: ${message}`
  } finally {
    initialLoading.value = false
  }
}

async function loadDetails() {
  try {
    const historyMode = (state.value.running ? state.value.mode : selectedMode.value).toLowerCase()
    const [historyData, logsData] = await Promise.all([
      api(`/api/${historyMode}/history?limit=500`),
      api('/api/demo/logs?limit=500'),
    ])
    history.value = Array.isArray(historyData.items) ? historyData.items : []
    logs.value = Array.isArray(logsData.items) ? logsData.items : []
  } catch (error) {
    if (error instanceof TypeError) backendAvailable.value = false
    backendError.value = error instanceof Error ? error.message : String(error)
  }
}

async function loadStrategyConfig() {
  try {
    const config = await api('/api/demo/strategy-config')
    strategyConfig.value = { ...config, stakes: [...config.stakes] }
  } catch (error) {
    if (error instanceof TypeError) backendAvailable.value = false
    backendError.value = error instanceof Error ? error.message : String(error)
  }
}

function regenerateStakes() {
  const initial = Number(strategyConfig.value.initial_stake)
  const multiplier = Number(strategyConfig.value.progression_multiplier)
  const steps = Number(strategyConfig.value.max_steps)
  if (!Number.isFinite(initial) || !Number.isFinite(multiplier) || !Number.isInteger(steps) || initial <= 0 || multiplier <= 1 || steps < 1) return
  const stakes = [Math.round(initial * 100) / 100]
  while (stakes.length < steps) stakes.push(Math.floor(stakes[stakes.length - 1] * multiplier))
  strategyConfig.value.stakes = stakes
}

function updateStake(index, event) {
  strategyConfig.value.stakes[index] = Number(event.target.value)
}

async function saveStrategy() {
  pendingAction.value = 'save-strategy'
  try {
    const saved = await api('/api/demo/strategy-config', { method: 'PUT', body: JSON.stringify(strategyConfig.value) })
    strategyConfig.value = { ...saved, stakes: [...saved.stakes] }
    await loadState()
  } catch (error) {
    backendError.value = error instanceof Error ? error.message : String(error)
  } finally {
    pendingAction.value = null
  }
}

async function refresh() {
  if (refreshInFlight) return
  refreshInFlight = true
  try {
    await loadState()
    tick += 1
    if (backendAvailable.value && tick % 5 === 0) {
      await loadDetails()
    }
  } finally {
    refreshInFlight = false
  }
}

async function control(action) {
  pendingAction.value = action
  try {
    const endpointMode = action === 'start'
      ? selectedMode.value.toLowerCase()
      : action === 'stop'
        ? (state.value.mode || selectedMode.value).toLowerCase()
        : 'demo'
    state.value = await api(`/api/${endpointMode}/${action}`, { method: 'POST' })
    backendError.value = ''
    await loadDetails()
  } catch (error) {
    backendError.value = error instanceof Error ? error.message : String(error)
  } finally {
    pendingAction.value = null
  }
}

async function clearHistory() {
  pendingAction.value = 'clear-history'
  try {
    const result = await api('/api/demo/history', { method: 'DELETE' })
    history.value = Array.isArray(result.items) ? result.items : []
    if (result.state) state.value = result.state
    backendAvailable.value = true
    backendError.value = ''
    await loadDetails()
  } catch (error) {
    backendError.value = error instanceof Error ? error.message : String(error)
  } finally {
    pendingAction.value = null
  }
}

async function clearDatabase() {
  const confirmed = window.confirm(
    'Очистить всю DEMO-базу? История, логи, бюджет и настройки стратегии будут сброшены.'
  )
  if (!confirmed) return
  pendingAction.value = 'clear-database'
  try {
    const result = await api('/api/demo/database', { method: 'DELETE' })
    history.value = []
    logs.value = []
    if (result.state) {
      state.value = result.state
      strategyConfig.value = {
        ...result.state.strategy_config,
        stakes: [...result.state.strategy_config.stakes],
      }
    }
    backendAvailable.value = true
    backendError.value = ''
  } catch (error) {
    backendError.value = error instanceof Error ? error.message : String(error)
  } finally {
    pendingAction.value = null
  }
}

function show(value, fallback = '—') {
  return value === null || value === undefined || value === '' ? fallback : value
}

function formatNumber(value, digits = 2) {
  const number = Number(value)
  if (!Number.isFinite(number)) return '—'
  return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: digits }).format(number)
}

const match = computed(() => state.value.match || {})
const bet = computed(() => state.value.bet || {})
const stats = computed(() => ({ ...emptyStats, ...(state.value.stats || {}) }))
const lastChange = computed(() => state.value.last_change || {})
const scanner = computed(() => state.value.scanner || {})
const marketReader = computed(() => state.value.market_reader || {})
const budget = computed(() => state.value.budget || {})
const sequence = computed(() => state.value.sequence || {})
const strategyBudget = computed(() => strategyConfig.value.stakes.reduce((total, amount) => total + (Number(amount) || 0), 0))
const configInvalid = computed(() => {
  const config = strategyConfig.value
  return !(Number(config.initial_stake) > 0) || !(Number(config.progression_multiplier) > 1) || !Number.isInteger(Number(config.max_steps)) || Number(config.max_steps) < 1 || config.stakes.length !== Number(config.max_steps) || config.stakes.some((amount) => !Number.isFinite(Number(amount)) || Number(amount) <= 0)
})
const hasActiveBet = computed(() => (
  state.value.bet?.result === 'ACTIVE'
  || history.value.some((item) => (item.mode || 'DEMO') === 'DEMO' && item.result === 'ACTIVE')
))
const canStart = computed(() => backendAvailable.value && !initialLoading.value && !actionPending.value && !state.value.running && !configInvalid.value)
const canStop = computed(() => backendAvailable.value && !actionPending.value && state.value.running)
const canResetSequence = computed(() => backendAvailable.value && !actionPending.value && !state.value.running && selectedMode.value === 'DEMO')
const canClearHistory = computed(() => backendAvailable.value && !actionPending.value && !state.value.running && !hasActiveBet.value && selectedMode.value === 'DEMO')
const canClearDatabase = computed(() => backendAvailable.value && !actionPending.value && !state.value.running && !hasActiveBet.value)
const reversedHistory = computed(() => [...history.value].reverse())
const recentLogs = computed(() => logs.value.slice(-250))
const profitTone = computed(() => Number(budget.value.session_profit || 0) >= 0 ? 'green' : 'red')
const signedProfit = computed(() => {
  const value = Number(budget.value.session_profit)
  if (!Number.isFinite(value)) return '—'
  return `${value >= 0 ? '+' : ''}${formatNumber(value)}`
})
const authStatus = computed(() => state.value.auth?.status || 'UNKNOWN')
const authStage = computed(() => {
  if (authStatus.value === 'AUTHORIZED') {
    return {
      tone: 'authorized',
      title: 'Авторизация выполнена',
      message: 'Вход подтверждён по блоку баланса. Бот продолжил работу автоматически.',
      status: 'Авторизован',
    }
  }
  if (authStatus.value === 'WAITING_MANUAL_LOGIN') {
    return {
      tone: 'waiting',
      title: 'Авторизация',
      message: 'Браузер открыт. Войдите на сайте вручную.',
      status: 'Ожидание входа',
    }
  }
  return {
    tone: 'checking',
    title: 'Авторизация',
    message: 'Открываем сайт и проверяем сохранённую сессию.',
    status: 'Проверка авторизации',
  }
})
const scoreText = computed(() => {
  if (match.value.score1 === null || match.value.score1 === undefined) return '— : —'
  return `${match.value.score1} : ${match.value.score2}`
})
const statusTone = computed(() => {
  if (state.value.status === 'ERROR') return 'danger'
  if (state.value.status === 'STOPPED') return 'neutral'
  if (state.value.status === 'WAITING_MANUAL_LOGIN') return 'warning'
  if (state.value.status === 'AUTHORIZED') return 'success'
  if (['WIN', 'GOAL_DETECTED'].includes(state.value.status)) return 'success'
  if (['LOSE', 'SEQUENCE_EXHAUSTED'].includes(state.value.status)) return 'warning'
  return 'active'
})
const updatedAt = computed(() => {
  if (!state.value.updated_at) return '—'
  const date = new Date(state.value.updated_at)
  return Number.isNaN(date.getTime()) ? state.value.updated_at : date.toLocaleTimeString('ru-RU')
})

onMounted(async () => {
  await Promise.all([loadState(), loadDetails(), loadStrategyConfig()])
  timer = setInterval(refresh, 1000)
})

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <div class="brand">
        <div class="brand-mark">AB</div>
        <div>
          <div class="brand-title">{{ state.running ? state.mode : selectedMode }} MONITOR</div>
          <div class="brand-subtitle">AutoBet · FC 25 Live Strategy</div>
        </div>
      </div>

      <div class="controls">
        <select v-model="selectedMode" class="mode-select" :disabled="state.running || actionPending">
          <option value="DEMO">DEMO</option>
          <option value="LIVE">LIVE</option>
        </select>
        <div class="demo-chip">{{ state.running ? state.mode : selectedMode }}</div>
        <button
          :class="['button', selectedMode === 'LIVE' ? 'button-danger' : 'button-start']"
          :disabled="!canStart"
          @click="control('start')"
        >
          {{ pendingAction === 'start' ? 'Запуск...' : `Запустить ${selectedMode}` }}
        </button>
        <button
          class="button button-stop"
          :disabled="!canStop"
          @click="control('stop')"
        >
          Остановить
        </button>
        <button class="button" :disabled="!canResetSequence" @click="control('reset')">Новая серия</button>
        <button class="button button-danger" :disabled="!canClearDatabase" @click="clearDatabase">
          {{ pendingAction === 'clear-database' ? 'Очистка...' : 'Очистить базу данных' }}
        </button>
      </div>
    </header>

    <main class="dashboard">
      <section :class="['demo-notice', { 'live-notice': (state.running ? state.mode : selectedMode) === 'LIVE' }]">
        <span class="notice-icon">{{ (state.running ? state.mode : selectedMode) === 'LIVE' ? 'L' : 'D' }}</span>
        <div>
          <strong>{{ (state.running ? state.mode : selectedMode) }}-РЕЖИМ</strong>
          <span v-if="(state.running ? state.mode : selectedMode) === 'LIVE'">Coupon заполняется автоматически, но кнопку «Сделать ставку» нажимает пользователь.</span>
          <span v-else>Реальные ставки не отправляются. Все действия стратегии виртуальные.</span>
        </div>
      </section>

      <section v-if="state.status === 'READY_FOR_MANUAL_CONFIRMATION'" class="auth-stage auth-stage-waiting">
        <div>
          <strong>Подтвердите LIVE-ставку</strong>
          <span>Проверьте coupon и один раз нажмите «Сделать ставку» в окне букмекера.</span>
        </div>
        <small>Шаг {{ bet.step }} · {{ bet.amount }} RUB · гол №{{ bet.next_goal_number }}</small>
      </section>

      <section
        v-if="state.running"
        :class="['auth-stage', `auth-stage-${authStage.tone}`]"
      >
        <div>
          <strong>{{ authStage.title }}</strong>
          <span>{{ authStage.message }}</span>
        </div>
        <small>Статус: {{ authStage.status }}</small>
      </section>

      <div v-if="backendError" class="alert alert-error">
        <strong>Backend недоступен</strong>
        <span>{{ backendError }}</span>
      </div>

      <div v-if="state.error" class="alert alert-error">
        <strong>{{ state.status }}</strong>
        <span>{{ state.error }}</span>
      </div>

      <section class="strategy-config panel">
        <header class="panel-header compact">
          <div><span class="eyebrow amber">STRATEGY</span><h2>Настройка стратегии</h2></div>
          <strong :class="configInvalid ? 'red' : 'green'">{{ configInvalid ? 'Проверьте значения' : `Бюджет ряда: ${formatNumber(strategyBudget)} ₽` }}</strong>
        </header>
        <div class="strategy-config-body">
          <label>Первоначальная ставка<input v-model.number="strategyConfig.initial_stake" type="number" min="0.01" step="0.01" @change="regenerateStakes"></label>
          <label>Множитель следующей ставки<input v-model.number="strategyConfig.progression_multiplier" type="number" min="1.01" step="0.01" @change="regenerateStakes"></label>
          <label>Количество шагов<input v-model.number="strategyConfig.max_steps" type="number" min="1" step="1" @change="regenerateStakes"></label>
          <button class="button" :disabled="state.running || actionPending" @click="regenerateStakes">Рассчитать ряд</button>
        </div>
        <div class="stake-row">
          <label v-for="(stake, index) in strategyConfig.stakes" :key="index">Шаг {{ index + 1 }}<input :value="stake" type="number" min="0.01" step="0.01" @input="updateStake(index, $event)"></label>
        </div>
        <footer class="strategy-config-footer">
          <span>Сохранённый ряд применяется при следующем запуске. Вручную изменённые суммы имеют приоритет.</span>
          <button class="button button-start" :disabled="state.running || actionPending || configInvalid" @click="saveStrategy">Сохранить настройки</button>
        </footer>
      </section>

      <section class="overview-grid">
        <article class="metric-card">
          <span>BROWSER</span>
          <strong :class="state.browser?.status === 'OPEN' ? 'tone-success' : 'tone-danger'">
            <i class="status-dot" />{{ show(state.browser?.status, 'CLOSED') }}
          </strong>
        </article>
        <article class="metric-card">
          <span>DEMO WORKER</span>
          <strong :class="state.running ? 'tone-active' : 'tone-neutral'">
            <i class="status-dot" />{{ state.running ? 'RUNNING' : 'STOPPED' }}
          </strong>
        </article>
        <article class="metric-card">
          <span>AUTH</span>
          <strong>{{ show(state.auth?.status, 'UNKNOWN') }}</strong>
        </article>
        <article class="metric-card">
          <span>STRATEGY STATE</span>
          <strong :class="`tone-${statusTone}`"><i class="status-dot" />{{ state.status }}</strong>
        </article>
      </section>

      <section class="status-line">
        <span :class="`pulse tone-${statusTone}`" />
        <strong>{{ state.message }}</strong>
        <span class="status-context">{{ state.mode }} · {{ state.league }} · {{ updatedAt }}</span>
      </section>

      <div v-if="initialLoading" class="loading-card">Получаем состояние DEMO worker...</div>

      <template v-else>
        <section class="scanner-grid">
          <article class="metric-card"><span>НАЙДЕНО ВСЕГО</span><strong>{{ scanner.total || 0 }}</strong></article>
          <article class="metric-card"><span>УЖЕ ИДЁТ</span><strong>{{ scanner.started || 0 }}</strong></article>
          <article class="metric-card"><span>ПРЕДСТОЯЩИХ</span><strong>{{ scanner.upcoming || 0 }}</strong></article>
          <article class="metric-card scanner-selected">
            <span>БЛИЖАЙШИЙ МАТЧ</span>
            <strong v-if="scanner.selected">
              {{ scanner.selected.team1 }} — {{ scanner.selected.team2 }} · {{ scanner.selected.time }}
            </strong>
            <strong v-else>—</strong>
          </article>
        </section>

        <section class="primary-grid">
          <article class="panel match-panel">
            <header class="panel-header">
              <div>
                <span class="eyebrow">LIVE SCOREBOARD</span>
                <h2>Текущий матч</h2>
              </div>
              <span class="timer">{{ show(match.timer) }}</span>
            </header>

            <div v-if="state.match" class="scoreboard">
              <div class="team team-left">
                <span>КОМАНДА 1</span>
                <strong>{{ show(match.team1) }}</strong>
              </div>
              <div class="score">
                <strong>{{ scoreText }}</strong>
                <span>{{ show(match.state, show(match.period, 'LIVE')) }}</span>
              </div>
              <div class="team team-right">
                <span>КОМАНДА 2</span>
                <strong>{{ show(match.team2) }}</strong>
              </div>
            </div>
            <div v-else class="panel-empty">Матч ещё не выбран</div>
          </article>

          <article class="panel selection-panel">
            <header class="panel-header">
              <div>
                <span class="eyebrow amber">СТРАТЕГИЯ</span>
                <h2>Выбранная команда</h2>
              </div>
            </header>
            <div v-if="state.selected_team" class="selection-content">
              <div class="selected-team">{{ state.selected_team }}</div>
              <div class="reason">
                <span>ПРИЧИНА ВЫБОРА</span>
                <strong>{{ state.selection_reason }}</strong>
              </div>
              <div class="odds-compare">
                <div>
                  <span>{{ show(match.team1, 'TEAM 1') }}</span>
                  <strong>{{ show(state.odds?.team1) }}</strong>
                </div>
                <div>
                  <span>{{ show(match.team2, 'TEAM 2') }}</span>
                  <strong>{{ show(state.odds?.team2) }}</strong>
                </div>
              </div>
              <div class="selection-details">
                <span>{{ state.selected_side === 'TEAM_1' ? 'Команда 1 / левая' : 'Команда 2 / правая' }}</span>
                <span>КФ при выборе: {{ show(state.initial_selected_odds) }}</span>
                <span>Шаг: {{ bet.step || 1 }} / {{ bet.max_steps || 7 }}</span>
                <span>Ставка: {{ show(bet.amount, 20) }} ₽</span>
              </div>
              <div class="selection-proof">Команда зафиксирована по большему начальному КФ</div>
            </div>
            <div v-else class="panel-empty">Ожидаем коэффициенты рынка</div>

            <div class="ocr-meta">
              <div>
                <span>РЫНОК</span>
                <strong>{{ show(state.odds?.market, bet.market) }}</strong>
              </div>
              <div>
                <span>ИСТОЧНИК</span>
                <strong>{{ show(marketReader.source, 'DOM / Playwright') }}</strong>
              </div>
              <div>
                <span>СТАТУС РЫНКА</span>
                <strong>{{ show(marketReader.status, state.odds?.status) }}</strong>
              </div>
              <div>
                <span>СЛЕДУЮЩИЙ ГОЛ</span>
                <strong>№{{ show(marketReader.next_goal_number, bet.next_goal_number) }}</strong>
              </div>
              <div class="ocr-status">
                <span>ЧТЕНИЕ</span>
                <strong>Playwright DOM · попытка {{ marketReader.attempt || 0 }}</strong>
                <small>Canvas/OCR для рынка «Следующий гол» не используется.</small>
              </div>
            </div>
          </article>
        </section>

        <section class="secondary-grid">
          <article class="panel bet-panel">
            <header class="panel-header compact">
              <div>
                <span class="eyebrow">VIRTUAL BET</span>
                <h2>Текущая ставка</h2>
              </div>
              <span class="step-badge">{{ bet.step || 0 }} / {{ bet.max_steps || 7 }}</span>
            </header>
            <div class="bet-grid">
              <div><span>СУММА</span><strong>{{ show(bet.amount) }} <small>RUB</small></strong></div>
              <div><span>МАТЧ</span><strong>{{ show(bet.match, state.match ? `${match.team1} — ${match.team2}` : null) }}</strong></div>
              <div><span>РЫНОК</span><strong>{{ show(bet.market, 'Следующий гол') }}</strong></div>
              <div><span>КОМАНДА</span><strong>{{ show(state.selected_team) }}</strong></div>
              <div><span>СТОРОНА</span><strong>{{ show(bet.side_label) }}</strong></div>
              <div><span>КОЭФФИЦИЕНТ</span><strong class="green">{{ show(bet.odds) }}</strong></div>
              <div><span>СЧЁТ ПЕРЕД СТАВКОЙ</span><strong>{{ show(bet.score_before) }}</strong></div>
              <div><span>НОМЕР СЛЕДУЮЩЕГО ГОЛА</span><strong>{{ show(bet.next_goal_number) }}</strong></div>
              <div><span>STATUS</span><strong>{{ show(bet.status, state.status) }}</strong></div>
            </div>
          </article>

          <article class="panel change-panel">
            <header class="panel-header compact">
              <div>
                <span class="eyebrow">LAST EVENT</span>
                <h2>Последнее изменение</h2>
              </div>
              <span
                v-if="lastChange.result"
                :class="['result-badge', String(lastChange.result).toLowerCase()]"
              >{{ lastChange.result }}</span>
            </header>
            <div v-if="state.last_change" class="change-content">
              <div class="score-change">
                <span>{{ lastChange.before }}</span><i>→</i><strong>{{ lastChange.after }}</strong>
              </div>
              <div class="goal-scorer">
                <span>КТО ЗАБИЛ</span>
                <strong>{{ lastChange.scorer }}</strong>
              </div>
            </div>
            <div v-else class="panel-empty">Изменений счёта пока нет</div>
          </article>
        </section>

        <section class="budget-section">
          <article>
            <span>НАЧАЛЬНЫЙ БЮДЖЕТ</span>
            <strong>{{ formatNumber(budget.initial_budget) }} ₽</strong>
          </article>
          <article>
            <span>ТЕКУЩИЙ БЮДЖЕТ</span>
            <strong>{{ formatNumber(budget.current_budget) }} ₽</strong>
          </article>
          <article>
            <span>РЕЗУЛЬТАТ СЕССИИ</span>
            <strong :class="profitTone">{{ signedProfit }} ₽</strong>
          </article>
          <article>
            <span>P&L ТЕКУЩЕЙ СЕРИИ · ШАГ {{ sequence.current_step || 1 }}</span>
            <strong :class="Number(sequence.cumulative_pnl || 0) >= 0 ? 'green' : 'red'">{{ Number(sequence.cumulative_pnl || 0) >= 0 ? '+' : '' }}{{ formatNumber(sequence.cumulative_pnl || 0) }} ₽</strong>
          </article>
        </section>

        <section class="stats-section">
          <div class="section-heading">
            <span class="eyebrow">DEMO ANALYTICS</span>
            <h2>Статистика стратегии</h2>
          </div>
          <div class="stats-grid">
            <article><span>Матчей обработано</span><strong>{{ stats.matches_processed }}</strong></article>
            <article><span>Ставок сделано</span><strong>{{ stats.bets }}</strong></article>
            <article><span>WIN</span><strong class="green">{{ stats.wins }}</strong></article>
            <article><span>LOSE</span><strong class="red">{{ stats.losses }}</strong></article>
            <article><span>Сумма demo-ставок</span><strong>{{ formatNumber(stats.total_amount) }} ₽</strong></article>
            <article><span>Средний КФ</span><strong>{{ formatNumber(stats.average_odds, 3) }}</strong></article>
            <article><span>Макс. шаг</span><strong>{{ stats.max_step }}</strong></article>
            <article><span>Шагов до WIN</span><strong>{{ formatNumber(stats.average_steps_to_win) }}</strong></article>
          </div>
        </section>

        <section class="data-grid">
          <article class="panel history-panel">
            <header class="panel-header compact">
              <div>
                <span class="eyebrow">BET JOURNAL</span>
                <h2>История ставок</h2>
              </div>
              <div class="history-actions">
                <button class="button" :disabled="!canClearHistory" @click="clearHistory">
                  {{ pendingAction === 'clear-history' ? 'Очищение...' : 'Очистить историю ставок' }}
                </button>
                <span class="counter">{{ history.length }}</span>
              </div>
            </header>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>№</th><th>Матч</th><th>Команда</th><th>Ставка</th><th>КФ</th>
                    <th>Шаг</th><th>Счёт до</th><th>Счёт после</th><th>Кто забил</th><th>Результат</th>
                    <th>Бюджет до</th><th>Изменение</th><th>Бюджет после</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(item, index) in reversedHistory" :key="item.id || `${item.cycle_id}-${item.step}`">
                    <td>{{ history.length - index }}</td>
                    <td>{{ item.match }}</td>
                    <td>{{ item.selected_team }}</td>
                    <td>{{ item.amount }} ₽</td>
                    <td>{{ item.odds }}</td>
                    <td>{{ item.step }}</td>
                    <td>{{ item.score_before }}</td>
                    <td>{{ show(item.score_after) }}</td>
                    <td>{{ show(item.scorer) }}</td>
                    <td><span :class="['table-result', String(item.result).toLowerCase()]">{{ item.result }}</span></td>
                    <td>{{ formatNumber(item.budget_before) }} ₽</td>
                    <td :class="Number(item.budget_change) >= 0 ? 'green' : 'red'">
                      {{ item.budget_change === null || item.budget_change === undefined ? '—' : `${Number(item.budget_change) >= 0 ? '+' : ''}${formatNumber(item.budget_change)} ₽` }}
                    </td>
                    <td>{{ item.budget_after === null || item.budget_after === undefined ? '—' : `${formatNumber(item.budget_after)} ₽` }}</td>
                  </tr>
                  <tr v-if="!history.length"><td colspan="13" class="empty-row">Виртуальных ставок пока нет</td></tr>
                </tbody>
              </table>
            </div>
          </article>

          <article class="panel log-panel">
            <header class="panel-header compact">
              <div>
                <span class="eyebrow">LIVE STREAM</span>
                <h2>Живой лог</h2>
              </div>
              <span class="live-indicator"><i /> LIVE</span>
            </header>
            <div class="logs">
              <div v-for="(item, index) in recentLogs" :key="`${item.timestamp}-${index}`" class="log-row">
                <time>{{ item.time }}</time>
                <span class="log-event">{{ item.event }}</span>
                <p>{{ item.message }}</p>
              </div>
              <div v-if="!logs.length" class="panel-empty">Лог появится после запуска DEMO</div>
            </div>
          </article>
        </section>
      </template>
    </main>
  </div>
</template>
