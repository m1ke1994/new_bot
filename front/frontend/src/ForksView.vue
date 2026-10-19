<script setup>
import { computed, onMounted, onBeforeUnmount, ref } from 'vue'
import { apiUrl } from './config'
const budget = ref(5000)
const fraction = ref(10)
const state = ref({ running:false, phase:'STOPPED', message:'Остановлен', balance:0, initial_budget:0, session_profit:0, allocated_percent:10, match:null, quote:null, position:null, scanned:0, signals:0 })
const history = ref([])
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
      const [h, l] = await Promise.all([api('/api/forks/history?limit=300'), api('/api/forks/logs?limit=200')])
      history.value = h.items || []
      logs.value = l.items || []
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
      ? { method:'POST', body:JSON.stringify({ budget:Number(budget.value), allocation_percent:Number(fraction.value) }) }
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
const preview = computed(() => {
  const a = Number(quote.value.odd1), b = Number(quote.value.odd2)
  if (!(a > 1 && b > 1)) return null
  const total = Math.floor(Number(state.value.balance) * Number(state.value.allocated_percent) ) / 100
  const k = 1/a + 1/b
  const stake1 = Math.floor(total * (1/a) / k * 100) / 100
  const stake2 = Math.round((total-stake1)*100)/100
  const profit1 = Math.floor(stake1*a*100)/100-total
  const profit2 = Math.floor(stake2*b*100)/100-total
  return { total, stake1, stake2, best:Math.min(profit1,profit2) }
})
const stage = computed(() => ({
  STOPPED:'Остановлен', AUTH_CHECK:'Ожидание авторизации', SEARCHING_MATCH:'Поиск ближайшего матча',
  OPENING_MATCH:'Открытие матча', WAITING_TIMER:'Чтение таймера', WAITING_WINDOW:'Ожидание 05:00',
  READING_ODDS:'Canvas: чтение коэффициентов', WAITING_MARKET:'Рынок недоступен',
  WATCHING_QUOTES:'Ожидание вилки', WAITING_RESULT:'Ждём следующий гол',
  SETTLED:'Рассчитано', NEEDS_REVIEW:'Требуется проверка',
  RECOVERING_BROWSER:'Восстановление Chromium', WAITING_AUTH:'Ожидание входа',
  WAITING_CARDS:'Ожидание карточек матчей', NO_MATCH_CARDS:'Карточки отсутствуют',
  NO_UPCOMING_MATCHES:'Ожидание ближайшего матча', ERROR:'Ошибка сканирования',
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
    <div class="fork-brand"><b>F//</b><span>FORK WATCH <em>VIRTUAL</em><small>FC25 · Conference League · 05:00–05:30</small></span></div>
    <router-link to="/" class="fork-back">← Основная стратегия и фильтры</router-link>
  </header>
  <main class="fork-wrap">
    <div class="fork-heading"><div><small>АНАЛИТИКА / СЛЕДУЮЩИЙ ГОЛ</small><h1>Мониторинг поздних вилок</h1><p>Выбор ближайшего матча, ожидание 05:00, наблюдение коэффициентов до 05:30 и исключительно виртуальная фиксация.</p></div><strong>{{ state.running ? '● НАБЛЮДЕНИЕ ИДЁТ' : '○ ОСТАНОВЛЕН' }}</strong></div>
    <div class="fork-warning"><b>Это условная вилка, не безрисковый арбитраж.</b> Исход «больше не будет голов» не покрыт. При нём обе ставки проигрывают, даже если сумма обратных коэффициентов меньше 1. Реальных ставок нет.</div>
    <div v-if="error" class="fork-error">{{ error }}</div>
    <section class="fork-main-grid">
      <article class="fork-card">
        <div class="fork-label">01 / БЮДЖЕТ <span>RUB</span></div>
        <label>Стартовый виртуальный бюджет<input v-model.number="budget" type="number" min="20" step="100" :disabled="state.running"></label>
        <label>Доля доступного бюджета на пару, %<input v-model.number="fraction" type="number" min="1" max="100" :disabled="state.running"></label>
        <button :class="['fork-btn', state.running ? 'stop' : 'start']" :disabled="busy" @click="control(state.running ? 'stop' : 'start')">{{ state.running ? '■ Остановить' : '▶ Запустить мониторинг' }}</button>
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
        <div class="fork-timer">{{ match.timer || '—:—' }} <small>Окно входа: 05:00 — 05:30</small></div>
        <p class="fork-muted">Счёт: {{ match.score || '—' }} · Следующий гол №{{ quote.goal_number ?? '—' }}</p>
        <div class="fork-odds"><div><small>{{ quote.team1 || 'Команда 1' }}</small><strong>{{ odd(quote.odd1) }}</strong></div><div><small>{{ quote.team2 || 'Команда 2' }}</small><strong>{{ odd(quote.odd2) }}</strong></div></div>
        <div :class="['fork-indicator',{positive:signal}]"><b>{{ quote.blocked ? 'Рынок заблокирован' : signal ? 'Условная вилка обнаружена' : 'Ожидаем соотношение коэффициентов' }}</b><span>Обратные коэффициенты: {{ Number.isFinite(quote.inverse_sum) ? (quote.inverse_sum * 100).toFixed(2) + '%' : '—' }} · {{ quote.source || 'Источник неизвестен' }}</span></div>
        <p class="fork-muted">{{ state.message }}</p>
      </article>
      <article class="fork-card">
        <div class="fork-label">03 / РАСПРЕДЕЛЕНИЕ <span>DEMO</span></div>
        <div v-if="position">
          <h2>{{ position.match }}</h2><small>Зафиксированная пара · {{ position.status }}</small>
          <div class="fork-line"><span>{{ position.team1 }} @ {{ odd(position.odd1) }}</span><b>{{ rub(position.stake1) }}</b></div>
          <div class="fork-line"><span>{{ position.team2 }} @ {{ odd(position.odd2) }}</span><b>{{ rub(position.stake2) }}</b></div>
          <div class="fork-line"><span>Всего виртуально выделено</span><b>{{ rub(position.total_stake) }}</b></div>
          <div class="fork-line"><span>Мин. прибыль при следующем голе</span><b class="green">{{ rub(position.minimum_conditional_profit) }}</b></div>
          <div class="fork-line"><span>Убыток без гола</span><b class="red">{{ rub(position.no_goal_loss) }}</b></div>
        </div>
        <div v-else-if="preview">
          <p class="fork-muted">Предварительный расчёт — ещё не виртуальная ставка</p>
          <div class="fork-line"><span>{{ quote.team1 }} @ {{ odd(quote.odd1) }}</span><b>{{ rub(preview.stake1) }}</b></div>
          <div class="fork-line"><span>{{ quote.team2 }} @ {{ odd(quote.odd2) }}</span><b>{{ rub(preview.stake2) }}</b></div>
          <div class="fork-line"><span>На пару</span><b>{{ rub(preview.total) }}</b></div>
          <div class="fork-line"><span>Минимум при голе</span><b :class="preview.best > 0 ? 'green':'red'">{{ rub(preview.best) }}</b></div>
          <div class="fork-line"><span>Если голов больше нет</span><b class="red">{{ rub(-preview.total) }}</b></div>
        </div>
        <p v-else class="fork-muted">Ожидаем коэффициенты для расчёта сумм.</p>
        <p class="fork-muted">Пара записывается только при одновременных незаблокированных исходах, неизменном счёте, точном времени и положительной расчётной прибыли после округления.</p>
      </article>
    </section>
    <section class="fork-bottom">
      <article class="fork-card">
        <div class="fork-label">04 / ИСТОРИЯ <span>{{ history.length }} записей</span></div>
        <div class="fork-table-scroll"><table><thead><tr><th>Время</th><th>Матч</th><th>Следующий гол</th><th>Сторона 1</th><th>Сторона 2</th><th>Результат</th><th>P/L</th></tr></thead><tbody>
          <tr v-for="item in history" :key="item.id"><td>{{ date(item.created_at) }}</td><td><b>{{ item.match }}</b><small>{{ item.score_before }} → {{ item.score_after || '—' }}</small></td><td>№{{ item.goal_number }} / {{ item.timer }}</td><td>{{ rub(item.stake1) }}<small>@ {{ odd(item.odd1) }}</small></td><td>{{ rub(item.stake2) }}<small>@ {{ odd(item.odd2) }}</small></td><td>{{ item.result || item.status }}</td><td :class="item.profit===null?'':Number(item.profit)>=0?'green':'red'">{{ rub(item.profit) }}</td></tr>
          <tr v-if="!history.length"><td colspan="7">Сигналов пока нет</td></tr>
        </tbody></table></div>
      </article>
      <article class="fork-card">
        <div class="fork-label">05 / ЖИВЫЕ ЛОГИ <span>200</span></div>
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
.fork-bottom{display:grid;grid-template-columns:1.7fr 1fr;gap:16px}
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
