<script setup>
import { computed, onMounted, onBeforeUnmount, ref } from 'vue'
import { apiUrl } from './config'
const budget = ref(5000)
const fraction = ref(10)
const placementMode = ref('VIRTUAL')
const confirmDemo = ref(false)
const state = ref({ running:false, phase:'STOPPED', message:'Остановлен', balance:0, initial_budget:0, session_profit:0, allocated_percent:10, match:null, quote:null, position:null, scanned:0, signals:0 })
const history = ref([])
const observations = ref([])
const logs = ref([])
const error = ref('')
const busy = ref(false)
let poller, ticks = 0, inflight = false
async function api(path, options = {}) {
  const response = await fetch(apiUrl(path), { ...options, headers: { 'Content-Type':'application/json' } })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}))
    throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Ошибка сервера: ' + response.status)
  }
  return response.json()
}
async function refresh() {
  if (inflight) return
  inflight = true
  try {
    state.value = await api('/api/forks/state')
    if (ticks++ % 3 === 0) {
      const [h, l, o] = await Promise.all([
        api('/api/forks/history?limit=200'),
        api('/api/forks/logs?limit=200'),
        api('/api/forks/observations?limit=300'),
      ])
      history.value = h.items || []
      logs.value = l.items || []
      observations.value = o.items || []
    }
    error.value = ''
  } catch (err) { error.value = String(err.message || err) }
  finally { inflight = false }
}
async function control(mode) {
  if (busy.value) return
  busy.value = true
  try {
    const options = mode === 'start'
      ? { method:'POST', body:JSON.stringify({
          budget:Number(budget.value), allocation_percent:Number(fraction.value),
          placement_mode:placementMode.value, confirm_demo:confirmDemo.value,
        }) }
      : { method:'POST' }
    await api('/api/forks/' + mode, options)
    ticks = 0
    await refresh()
  } catch (err) { error.value = String(err.message || err) }
  finally { busy.value = false }
}
const quote = computed(() => state.value.quote || {})
const position = computed(() => state.value.position)
const match = computed(() => state.value.match || {})
const signal = computed(() => quote.value.conditional_signal && !quote.value.blocked)
const targetCount = computed(() => Number(state.value.target_pairs || 200))
const completedCount = computed(() => Number(state.value.completed_pairs || 0))
const targetProgress = computed(() =>
  Math.min(100, Math.round(completedCount.value / Math.max(1, targetCount.value) * 100))
)
const preview = computed(() => {
  const a = Number(quote.value.odd1), b = Number(quote.value.odd2)
  if (!(a > 1 && b > 1)) return null
  const total = Math.floor(Number(state.value.balance) * Number(state.value.allocated_percent) / 100)
  const k = 1/a + 1/b
  if (total < 20 || k >= 1) return null
  const ideal = Math.floor(total * (1/a) / k + 0.5)
  let best = null
  for (const offset of [-2, -1, 0, 1, 2]) {
    const stake1 = ideal + offset
    const stake2 = total - stake1
    if (stake1 < 10 || stake2 < 10) continue
    const payout1 = Math.floor(stake1 * a * 100) / 100
    const payout2 = Math.floor(stake2 * b * 100) / 100
    const profitMin = Math.min(payout1, payout2) - total
    if (profitMin <= 0) continue
    if (!best || profitMin > best.best) {
      best = { total, stake1, stake2, best: profitMin }
    }
  }
  return best
})
const stage = computed(() => ({
  STOPPED:'Остановлен', AUTH_CHECK:'Проверка авторизации',
  OPENING_SITE:'Открытие сайта в Chromium', SITE_ERROR:'Ошибка открытия сайта',
  SEARCHING_MATCH:'Поиск ближайшего матча',
  OPENING_MATCH:'Открытие будущего матча', WAITING_MATCH_START:'Ожидание начала LIVE',
  WAITING_TIMER:'Чтение игрового времени', WAITING_WINDOW:'Ожидание LIVE 05:00',
  OBSERVING_ODDS:'Чтение коэффициентов до 05:00 (без ставок)',
  READING_ODDS:'Canvas: чтение коэффициентов', WAITING_MARKET:'Рынок недоступен',
  WATCHING_QUOTES:'Ожидание вилки', WAITING_RESULT:'Ожидание гола · максимум 60 секунд',
  SETTLED:'Рассчитано', NEEDS_REVIEW:'Требуется проверка',
  RECOVERING_BROWSER:'Восстановление Chromium', WAITING_AUTH:'Ожидание входа',
  WAITING_CARDS:'Ожидание карточек матчей', NO_MATCH_CARDS:'Карточки отсутствуют',
  NO_UPCOMING_MATCHES:'Ожидание ближайшего матча', ERROR:'Ошибка сканирования',
  LIVE_HANDOFF:'Переход к уже идущему матчу',
  TARGET_REACHED:'Цель 200 виртуальных пар достигнута',
  BOOKMAKER_DEMO_PLACING:'БК DEMO: отправка двух купонов',
  BOOKMAKER_DEMO_HALT:'БК DEMO: проверка неполной пары',
  BOOKMAKER_DEMO_UNRESOLVED:'БК DEMO: купоны требуют проверки',
  DEMO_BALANCE_EXHAUSTED:'Учебный лимит исчерпан',
})[state.value.phase] || state.value.phase)
function rub(v) {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return '—'
  return Number(v).toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2}) + ' ₽'
}
function odd(v) { return Number.isFinite(Number(v)) ? Number(v).toFixed(3) : '—' }
function date(v) { return v ? new Date(v).toLocaleString('ru-RU') : '—' }
onMounted(() => { refresh(); poller = setInterval(refresh,900) })
onBeforeUnmount(() => { clearInterval(poller) })
</script>

<template>
<div class="fork-page">
  <header class="fork-top">
    <div class="fork-brand"><b>F//</b><span>FORK WATCH <em>VIRTUAL</em><small>FC25 · Conference League · 05:00–05:35</small></span></div>
    <router-link to="/" class="fork-back">← Основная стратегия и фильтры</router-link>
  </header>
  <main class="fork-wrap">
    <div class="fork-heading"><div><small>АНАЛИТИКА / СЛЕДУЮЩИЙ ГОЛ</small><h1>Мониторинг поздних вилок</h1><p>Выбор ближайшего матча, ожидание 05:00, наблюдение коэффициентов до 05:35, виртуальная симуляция или реальные клики только на подтверждённом учебном счёте букмекера.</p></div><strong>{{ state.running ? '● НАБЛЮДЕНИЕ ИДЁТ' : '○ ОСТАНОВЛЕН' }}</strong></div>
    <div class="fork-warning"><b>Это условная вилка, не безрисковый арбитраж.</b> Исход «больше не будет голов» не покрыт. При нём обе ставки проигрывают, даже если сумма обратных коэффициентов меньше 1. В режиме БК DEMO отправляются настоящие купоны на учебном счёте: не запускайте его без проверки типа счёта.</div>
    <div v-if="error" class="fork-error">{{ error }}</div>
    <section class="fork-main-grid">
      <article class="fork-card">
        <div class="fork-label">01 / БЮДЖЕТ <span>RUB</span></div>
        <label>Режим размещения
          <select v-model="placementMode" :disabled="state.running" class="fork-mode-select" @change="confirmDemo = false">
            <option value="VIRTUAL">Виртуально (без купонов)</option>
            <option value="BOOKMAKER_DEMO">БК DEMO (две ставки на учебном счёте)</option>
          </select>
        </label>
        <div v-if="placementMode === 'BOOKMAKER_DEMO'" class="fork-demo-mode">
          <p><strong>БК DEMO</strong> — сначала меньший коэффициент, после подтверждения первая сторона закрывает окно «Продолжить» / ×; затем пересчитывается и отправляется вторая.</p>
          <p>Используем проверку RUB из основной стратегии. RUB сам по себе не отличает учебный счёт от денежного — проверь выбранный счёт перед запуском.</p>
          <label class="fork-ack"><input v-model="confirmDemo" type="checkbox" :disabled="state.running">
            Подтверждаю, что в браузере выбран учебный счёт БК с балансом RUB
          </label>
        </div>
        <label>Лимит {{ placementMode === 'BOOKMAKER_DEMO' ? 'учебного' : 'виртуального' }} бюджета<input v-model.number="budget" type="number" min="20" step="100" :disabled="state.running"></label>
        <label>Доля доступного бюджета на пару, %<input v-model.number="fraction" type="number" min="1" max="100" :disabled="state.running"></label>
        <button :class="['fork-btn', state.running ? 'stop' : 'start']" :disabled="busy || (!state.running && placementMode === 'BOOKMAKER_DEMO' && !confirmDemo) || (!state.running && placementMode === 'VIRTUAL' && completedCount >= targetCount)" @click="control(state.running ? 'stop' : 'start')">{{ state.running ? '■ Остановить' : placementMode === 'VIRTUAL' && completedCount >= targetCount ? '✓ Цель достигнута' : placementMode === 'BOOKMAKER_DEMO' ? '▶ Запустить БК DEMO' : '▶ Продолжить сбор вилок' }}</button>
        <div v-if="placementMode === 'VIRTUAL'" class="fork-target">
          <div class="fork-target-head"><span>Цель по завершённым виртуальным парам</span><strong>{{ completedCount }} / {{ targetCount }}</strong></div>
          <div class="fork-target-track"><div class="fork-target-fill" :style="{ width: targetProgress + '%' }"></div></div>
          <small>Осталось {{ Math.max(0, targetCount - completedCount) }}. Учтены предыдущие сессии, без дублирования или искусственных ставок.</small>
        </div>
        <div class="fork-metrics">
          <div><small>Доступно</small><strong>{{ rub(state.balance) }}</strong></div>
          <div><small>Прибыль сессии</small><strong :class="Number(state.session_profit)<0?'red':'green'">{{ rub(state.session_profit) }}</strong></div>
          <div><small>Матчей проверено</small><strong>{{ state.scanned }}</strong></div>
          <div><small>Виртуальных пар</small><strong>{{ state.signals }}</strong></div>
        </div>
      </article>
      <article class="fork-card">
        <div class="fork-label">02 / ТЕКУЩИЙ МАТЧ <span>{{ stage }}</span></div>
        <h2>{{ match.name || 'Ожидаем ближайший матч' }}</h2>
        <div class="fork-timer">{{ match.timer || '—:—' }} <small>{{ match.state === 'UPCOMING' ? 'Обратный отсчёт ДО начала — ожидаем LIVE' : match.state === 'LIVE' ? 'Игровое время LIVE · окно 05:00 — 05:35' : 'Ожидаем ближайший ещё не начавшийся матч' }}</small></div>
        <p class="fork-muted">Статус: {{ match.state === 'LIVE' ? 'LIVE' : match.state === 'UPCOMING' ? 'До начала' : '—' }} · {{ match.period || 'Тайм не определён' }} · Счёт: {{ match.score || '—' }} · Следующий гол №{{ quote.goal_number ?? match.next_goal_number ?? '—' }}</p>
        <div class="fork-odds"><div><small>{{ quote.team1 || match.team1 || 'Команда 1' }}</small><strong>{{ odd(quote.odd1) }}</strong></div><div><small>{{ quote.team2 || match.team2 || 'Команда 2' }}</small><strong>{{ odd(quote.odd2) }}</strong></div></div>
        <div :class="['fork-indicator',{positive:signal && quote.entry_window}]">
          <b>{{ quote.blocked ? 'Рынок заблокирован' : signal && quote.entry_window ? 'Условная вилка в разрешённом окне' : signal ? 'Условная вилка — только наблюдение до 05:00' : state.odds_error ? 'Не удалось прочитать коэффициенты' : quote.odd1 ? 'Коэффициенты наблюдаются, ждём окно входа' : 'Ожидаем коэффициенты и игровое время' }}</b>
          <span>Обратные коэффициенты: {{ Number.isFinite(quote.inverse_sum) ? (quote.inverse_sum * 100).toFixed(2) + '%' : '—' }} · {{ quote.source || 'Источник не определён' }}</span>
          <span v-if="quote.updated_at">Получены: {{ date(quote.updated_at) }} · {{ quote.entry_window ? 'Окно виртуальных ставок открыто' : 'Только мониторинг, виртуальные ставки запрещены' }}</span>
          <span v-if="state.odds_error">Диагностика Canvas: {{ state.odds_error }}</span>
          <span v-if="match.state === 'LIVE' && !match.timer">Таймер LIVE не найден в DOM; вход автоматически заблокирован</span>
        </div>
        <p class="fork-muted">{{ state.message }}</p>
        <p v-if="state.browser_url && (stage === 'Открытие сайта в Chromium' || stage === 'Ошибка открытия сайта' || stage === 'Проверка авторизации')" class="fork-muted">Вкладка: {{ state.browser_url }} · Цель: {{ state.target_url || 'не указана' }}</p>
      </article>
      <article class="fork-card">
        <div class="fork-label">03 / РАСПРЕДЕЛЕНИЕ <span>{{ state.placement_mode === 'BOOKMAKER_DEMO' ? 'БК DEMO' : 'VIRTUAL' }}</span></div>
        <div v-if="position">
          <h2>{{ position.match }}</h2><small>Зафиксированная пара · {{ position.status }}</small>
          <div v-if="position.status === 'PENDING' && state.result_timeout_remaining !== null" class="fork-countdown">
            До результата или тайм-аута: <b>{{ state.result_timeout_remaining }} сек</b>
          </div>
          <div class="fork-line"><span>Ставка 1 · {{ position.team1 }} @ {{ odd(position.odd1) }}</span><b>{{ rub(position.stake1) }} · {{ position.team1_result || 'PENDING' }}</b></div>
          <div class="fork-line"><span>Ставка 2 · {{ position.team2 }} @ {{ odd(position.odd2) }}</span><b>{{ rub(position.stake2) }} · {{ position.team2_result || 'PENDING' }}</b></div>
          <div class="fork-line"><span>Всего {{ position.placement_mode === 'BOOKMAKER_DEMO' ? 'запланировано в БК DEMO' : 'виртуально выделено' }}</span><b>{{ rub(position.total_stake) }}</b></div>
          <div v-if="position.placement_mode === 'BOOKMAKER_DEMO'" class="fork-disclaimer">
            Подтверждено учебных купонов: {{ position.accepted_legs || 0 }}/2.
            <div>Команда 1: {{ position.team1_placement || '—' }}; команда 2: {{ position.team2_placement || '—' }}.</div>
            <div v-if="position.placement_error">{{ position.placement_error }}</div>
            Выигрыш на табло не равен подтверждённой выплате БК.
          </div>
          <div class="fork-line"><span>Мин. прибыль при следующем голе</span><b class="green">{{ rub(position.minimum_conditional_profit) }}</b></div>
          <div class="fork-line"><span>Убыток без гола</span><b class="red">{{ rub(position.no_goal_loss) }}</b></div>
          <div v-if="position.result === 'TIMEOUT_60S'" class="fork-disclaimer">Симуляция: за 60 секунд новый гол не зафиксирован, обе виртуальные стороны получили LOSE. Это не официальный расчёт букмекерского рынка.</div>
          <div v-if="position.profit !== null && position.profit !== undefined" class="fork-line"><span>Зафиксированный виртуальный P/L</span><b :class="Number(position.profit)>=0 ? 'green' : 'red'">{{ rub(position.profit) }}</b></div>
        </div>
        <div v-else-if="preview">
          <p class="fork-muted">Предварительный расчёт в целых рублях — не виртуальная ставка. До 05:00 он только информационный. Третий исход не покрыт.</p>
          <div class="fork-line"><span>{{ quote.team1 }} @ {{ odd(quote.odd1) }}</span><b>{{ rub(preview.stake1) }}</b></div>
          <div class="fork-line"><span>{{ quote.team2 }} @ {{ odd(quote.odd2) }}</span><b>{{ rub(preview.stake2) }}</b></div>
          <div class="fork-line"><span>На пару</span><b>{{ rub(preview.total) }}</b></div>
          <div class="fork-line"><span>Минимум при голе</span><b :class="preview.best > 0 ? 'green':'red'">{{ rub(preview.best) }}</b></div>
          <div class="fork-line"><span>Если голов больше нет</span><b class="red">{{ rub(-preview.total) }}</b></div>
        </div>
        <p v-else class="fork-muted">Ожидаем коэффициенты для расчёта сумм.</p>
        <p class="fork-muted">Обе суммы округляются до целого рубля. Режим БК DEMO требует подтверждения учебного счёта, принятия каждого купона и повторной проверки цены второй стороны.</p>
      </article>
    </section>
    <section class="fork-card fork-observations">
      <div class="fork-label">04 / НАБЛЮДЕНИЕ: ГОЛЫ И ХРОНОЛОГИЯ <span>{{ observations.length }} событий</span></div>
      <p class="fork-muted">События фиксируются по изменениям scoreboard. Голы до входа в уже начавшийся матч не приписываются командам.</p>
      <div class="fork-table-scroll">
        <table>
          <thead><tr><th>Фиксация</th><th>Матч</th><th>Игровое время</th><th>Событие</th><th>Гол №</th><th>Кто забил</th><th>Счёт</th></tr></thead>
          <tbody>
            <tr v-for="(item, i) in observations" :key="i">
              <td>{{ date(item.timestamp) }}</td>
              <td>{{ item.match }}</td>
              <td>{{ item.period || '—' }} / {{ item.timer || '—' }}</td>
              <td>{{ item.event === 'GOAL' ? 'ГОЛ' : item.event === 'MATCH_JOINED' ? 'Вход в матч' : 'Изменение счёта' }}</td>
              <td>{{ item.goal_number || '—' }}</td>
              <td :class="{green:item.event === 'GOAL'}">{{ item.scorer_team || 'Не определена' }}</td>
              <td>{{ item.score_before || '—' }} → {{ item.score_after || '—' }}</td>
            </tr>
            <tr v-if="!observations.length"><td colspan="7">Ожидаем первый матч.</td></tr>
          </tbody>
        </table>
      </div>
    </section>
    <section class="fork-bottom">
      <article class="fork-card">
        <div class="fork-label">05 / ИСТОРИЯ ВИРТУАЛЬНЫХ ПАР <span>Последние {{ history.length }} из {{ state.records_total ?? history.length }} · цель {{ targetCount }}</span></div>
        <div class="fork-table-scroll"><table><thead><tr><th>Время</th><th>Матч</th><th>Следующий гол</th><th>Ставка 1</th><th>Ставка 2</th><th>Результат</th><th>P/L</th></tr></thead><tbody>
          <tr v-for="item in history" :key="item.id">
            <td>{{ date(item.created_at) }}</td>
            <td><b>{{ item.match }}</b><small>{{ item.score_before }} → {{ item.score_after || '—' }}</small></td>
            <td>№{{ item.goal_number }} / {{ item.timer }}</td>
            <td>{{ item.team1 }} · {{ rub(item.stake1) }}<small>@ {{ odd(item.odd1) }} · {{ item.team1_result || '—' }}</small></td>
            <td>{{ item.team2 }} · {{ rub(item.stake2) }}<small>@ {{ odd(item.odd2) }} · {{ item.team2_result || '—' }}</small></td>
            <td>{{ item.result || item.status }}<small v-if="item.settlement_basis === 'SIMULATED_60S_TIMEOUT'">Условный LOSE за 60 сек, не расчёт БК</small></td>
            <td :class="item.profit===null?'':Number(item.profit)>=0?'green':'red'">{{ rub(item.profit) }}</td>
          </tr>
          <tr v-if="!history.length"><td colspan="7">Сигналов пока нет</td></tr>
        </tbody></table></div>
      </article>
      <article class="fork-card">
        <div class="fork-label">06 / ЖИВЫЕ ЛОГИ <span>200</span></div>
        <div class="fork-log-scroll"><div v-for="(line,index) in [...logs].reverse()" :key="index" class="fork-log"><time>{{ date(line.timestamp) }}</time><b>{{ line.event }}</b><span>{{ line.message }}</span></div><p v-if="!logs.length" class="fork-muted">Запустите наблюдение, чтобы видеть события.</p></div>
      </article>
    </section>
  </main>
</div>
</template>

<style scoped>
.fork-page{min-height:100vh;background:#080d16;color:#edf4ff;font-family:Inter,system-ui,sans-serif}
.fork-page *{box-sizing:border-box}
.fork-top{display:flex;justify-content:space-between;align-items:center;gap:20px;padding:20px clamp(18px,4vw,64px);border-bottom:1px solid #253147;background:#0d1421}
.fork-brand{display:flex;gap:15px;align-items:center;font-weight:850;letter-spacing:1.2px}
.fork-brand>b{color:#5ce0af;border:1px solid #356350;border-radius:10px;padding:12px;font-size:20px}
.fork-brand em{color:#59dba4;font-size:10px;font-style:normal;margin-left:8px}
.fork-brand small{color:#8699b4;display:block;font-size:10px;margin-top:5px;font-weight:500;letter-spacing:0}
.fork-back{font-size:12px;color:#bed2ed;padding:12px 15px;border:1px solid #34455e;border-radius:9px;text-decoration:none}
.fork-wrap{max-width:1740px;padding:36px clamp(18px,4vw,64px) 70px;margin:auto}
.fork-heading{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:27px}
.fork-heading>div>small{color:#71a7da;font-size:10px;letter-spacing:2px}
.fork-heading h1{font-size:clamp(26px,3vw,46px);margin:13px 0;letter-spacing:-1px}
.fork-heading p{color:#95a8c0;font-size:12px;line-height:1.7;max-width:750px}
.fork-heading>strong{font-size:11px;border:1px solid #34455e;padding:12px;border-radius:20px;white-space:nowrap;color:#56d9a6}
.fork-warning{padding:16px 19px;border:1px solid #665237;background:#261e13;color:#e6cb9b;line-height:1.7;border-radius:11px;font-size:12px;margin-bottom:22px}
.fork-warning b{color:#ffecbd;display:block}
.fork-error{background:#351c29;color:#ff9fa9;border:1px solid #7b4350;border-radius:9px;padding:12px;margin-bottom:20px}
.fork-main-grid{display:grid;grid-template-columns:1fr 1.5fr 1.1fr;gap:16px;margin-bottom:16px}
.fork-card{min-width:0;border:1px solid #29394d;border-radius:13px;background:linear-gradient(145deg,#141e2b,#101723);padding:23px}
.fork-label{color:#82a7cb;letter-spacing:1.5px;font-size:10px;font-weight:800;display:flex;justify-content:space-between;gap:10px;margin-bottom:25px}
.fork-card label{color:#acbfd5;font-size:11px;display:block;margin:18px 0}
.fork-card input{display:block;background:#0b111b;border:1px solid #384960;color:#fff;border-radius:9px;width:100%;padding:13px;margin-top:8px;font-size:18px}
.fork-countdown{margin:10px 0;border:1px solid #34556a;background:#13283c;color:#cbecff;padding:11px;border-radius:9px;font-size:12px;display:flex;justify-content:space-between}
.fork-disclaimer{color:#e8bf84;background:#2f251a;border:1px solid #694d25;padding:10px;border-radius:9px;font-size:11px;line-height:1.6;margin:14px 0}
.fork-mode-select{width:100%;margin-top:8px;padding:12px;border:1px solid #334761;border-radius:9px;color:#e7f3ff;background:#101b2c}
.fork-demo-mode{border:1px solid #795b36;background:#2d241b;color:#ffdbad;border-radius:10px;padding:12px;margin:12px 0;font-size:11px;line-height:1.6}
.fork-demo-mode p{margin:0 0 8px}
.fork-ack{display:flex!important;align-items:start;gap:9px}
.fork-ack input{width:auto!important;min-width:16px;flex:none;margin-top:3px}
.fork-target{margin-top:18px;border:1px solid #30445d;background:#0f2030;padding:15px;border-radius:10px}
.fork-target-head{display:flex;gap:12px;align-items:center;justify-content:space-between;font-size:11px;color:#bed2e8}
.fork-target-head strong{font-size:17px;color:#50dda9;white-space:nowrap}
.fork-target-track{height:8px;background:#26384b;border-radius:12px;overflow:hidden;margin:12px 0}
.fork-target-fill{height:100%;background:#4cd9a7;transition:width .25s ease}
.fork-target small{color:#8fa9c0;line-height:1.55;font-size:10px;display:block}
.fork-btn{width:100%;border:0;border-radius:9px;padding:14px;margin-top:10px;font-size:12px;font-weight:800;cursor:pointer}
.fork-btn:disabled{opacity:.45;cursor:default}
.fork-btn.start{background:#49d6a2;color:#071910}.fork-btn.stop{background:#873b49;color:#fff}
.fork-metrics{display:grid;grid-template-columns:1fr 1fr;gap:23px 10px;border-top:1px solid #2c3b4e;margin-top:26px;padding-top:22px}
.fork-metrics small,.fork-odds small{font-size:10px;color:#8fa5bf;display:block;margin-bottom:6px}
.fork-metrics strong{font-size:18px}
.fork-card h2{font-size:20px;line-height:1.45;margin:0 0 18px}
.fork-timer{font-size:50px;letter-spacing:-2px;color:#4bdbac;font-weight:800;border-bottom:1px solid #29384e;padding:6px 0 18px}
.fork-timer small{font-size:12px;letter-spacing:0;color:#98aec7;display:block;font-weight:400;margin-top:5px}
.fork-muted{font-size:11px;color:#94a8c2;line-height:1.7;margin:15px 0}
.fork-odds{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.fork-odds>div{padding:15px;background:#0e1a2a;border:1px solid #344961;border-radius:10px}
.fork-odds strong{font-size:30px;display:block}
.fork-indicator{background:#252c36;border:1px solid #455060;border-radius:9px;padding:13px;margin-top:15px}
.fork-indicator.positive{background:#132d25;border-color:#2c9a70;color:#74ebbb}
.fork-indicator b,.fork-indicator span{display:block;font-size:11px;line-height:1.5}
.fork-indicator span{color:#a6b8d0;margin-top:5px}
.fork-line{display:flex;align-items:center;justify-content:space-between;gap:12px;border-bottom:1px solid #29394c;padding:14px 0;font-size:11px}
.fork-line span{color:#adbed3}.fork-line b{white-space:nowrap}
.green{color:#55dda9!important}.red{color:#ff8493!important}
.fork-observations{margin-bottom:16px}.fork-bottom{display:grid;grid-template-columns:1.7fr 1fr;gap:16px}
.fork-table-scroll{max-height:460px;overflow:auto}
table{border-collapse:collapse;width:100%;min-width:810px;text-align:left;font-size:11px}
td,th{padding:12px 9px;border-bottom:1px solid #29394c;white-space:nowrap}
th{font-weight:600;color:#8299b5}td{color:#d7e4f5}td small{display:block;color:#8397b1;margin-top:5px}
.fork-log-scroll{max-height:460px;overflow:auto}
.fork-log{display:grid;grid-template-columns:125px 95px 1fr;gap:9px;padding:12px 0;border-bottom:1px solid #29394c;align-items:start;font-size:10px}
.fork-log time{color:#88a0be}.fork-log b{color:#55dda9;overflow-wrap:anywhere}.fork-log span{color:#d5e1ee;overflow-wrap:anywhere;line-height:1.5}
@media(max-width:1150px){.fork-main-grid{grid-template-columns:1fr 1fr}.fork-main-grid>.fork-card:last-child{grid-column:span 2}.fork-bottom{grid-template-columns:1fr}}
@media(max-width:700px){.fork-main-grid{grid-template-columns:1fr}.fork-main-grid>.fork-card:last-child{grid-column:span 1}.fork-heading,.fork-top{flex-direction:column;align-items:flex-start}.fork-log{grid-template-columns:1fr;gap:3px}}
</style>
