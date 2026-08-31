<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

const emptyData = () => ({
  ok: false,
  league: '',
  updated_at: null,
  matches_count: 0,
  next_match: null,
  matches: [],
})

const data = ref(emptyData())

const loading = ref(true)
const error = ref('')
const connected = ref(false)

let timer = null


// ============================================================
// ЗАГРУЗКА ДАННЫХ
// ============================================================

async function loadMatches() {
  try {
    const response = await fetch(
      `/matches.json?t=${Date.now()}`,
      {
        cache: 'no-store',
      },
    )

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`)
    }

    const json = await response.json()

    if (!json || !Array.isArray(json.matches)) {
      throw new Error('Некорректный matches.json')
    }

    data.value = json

    connected.value = true
    error.value = ''
  } catch (err) {
    connected.value = false
    data.value = emptyData()

    error.value =
      err instanceof Error
        ? err.message
        : String(err)
  } finally {
    loading.value = false
  }
}


// ============================================================
// СОРТИРОВКА ПО ВРЕМЕНИ ДО НАЧАЛА
// ============================================================

const matches = computed(() => {
  return [...data.value.matches].sort((a, b) => {
    const aTime =
      Number.isFinite(Number(a.time_seconds))
        ? Number(a.time_seconds)
        : Number.MAX_SAFE_INTEGER

    const bTime =
      Number.isFinite(Number(b.time_seconds))
        ? Number(b.time_seconds)
        : Number.MAX_SAFE_INTEGER

    return aTime - bTime
  })
})


// ============================================================
// БЛИЖАЙШИЙ МАТЧ
// ============================================================

const nextMatch = computed(() => {
  if (data.value.next_match) {
    return data.value.next_match
  }

  return matches.value[0] || null
})


// ============================================================
// ОСТАЛЬНАЯ ОЧЕРЕДЬ
// ============================================================

const queueMatches = computed(() => {
  if (!nextMatch.value) {
    return matches.value
  }

  return matches.value.filter((match) => {
    if (
      match.match_id &&
      nextMatch.value.match_id
    ) {
      return (
        match.match_id !==
        nextMatch.value.match_id
      )
    }

    return (
      match.number !==
      nextMatch.value.number
    )
  })
})


// ============================================================
// КОЭФФИЦИЕНТ
// ============================================================

function displayOdd(value) {
  if (
    value === null ||
    value === undefined ||
    value === ''
  ) {
    return '—'
  }

  return value
}


// ============================================================
// АУТСАЙДЕР
//
// Чем БОЛЬШЕ коэффициент на победу,
// тем команда считается аутсайдером.
// ============================================================

function getOutsider(match) {
  if (!match) {
    return null
  }

  const odd1 = Number(match.odds_team1)
  const odd2 = Number(match.odds_team2)

  if (
    !Number.isFinite(odd1) ||
    !Number.isFinite(odd2)
  ) {
    return null
  }

  if (odd1 > odd2) {
    return {
      team: match.team1,
      odd: match.odds_team1,
      side: 1,
    }
  }

  if (odd2 > odd1) {
    return {
      team: match.team2,
      odd: match.odds_team2,
      side: 2,
    }
  }

  return null
}


const nextOutsider = computed(() => {
  return getOutsider(nextMatch.value)
})


// ============================================================
// ВРЕМЯ ОБНОВЛЕНИЯ
// ============================================================

const updatedAt = computed(() => {
  if (!data.value.updated_at) {
    return '—'
  }

  const date = new Date(data.value.updated_at)

  if (Number.isNaN(date.getTime())) {
    return data.value.updated_at
  }

  return date.toLocaleTimeString(
    'ru-RU',
    {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    },
  )
})


// ============================================================
// START
// ============================================================

onMounted(async () => {
  await loadMatches()

  timer = setInterval(
    loadMatches,
    2000,
  )
})


// ============================================================
// STOP
// ============================================================

onBeforeUnmount(() => {
  if (timer) {
    clearInterval(timer)
  }
})
</script>


<template>
  <div class="app">
    <!-- =================================================== -->
    <!-- HEADER -->
    <!-- =================================================== -->

    <header class="header">
      <div class="brand">
        <div class="brand-logo">
          AB
        </div>

        <div>
          <div class="brand-name">
            AutoBet
          </div>

          <div class="brand-description">
            FC 25 Match Automation
          </div>
        </div>
      </div>

      <div
        class="connection"
        :class="connected ? 'online' : 'offline'"
      >
        <span class="connection-dot" />

        <div>
          <strong>
            {{
              connected
                ? 'МОНИТОРИНГ АКТИВЕН'
                : 'НЕТ ДАННЫХ'
            }}
          </strong>

          <small>
            {{
              connected
                ? 'Мониторинг матчей'
                : 'Ожидание подключения'
            }}
          </small>
        </div>
      </div>
    </header>


    <!-- =================================================== -->
    <!-- CONTENT -->
    <!-- =================================================== -->

    <main class="main">
      <!-- ================================================= -->
      <!-- STATUS BAR -->
      <!-- ================================================= -->

      <section class="status-grid">
        <div class="status-card">
          <span class="status-label">
            СОСТОЯНИЕ
          </span>

          <strong class="status-green">
            {{
              connected
                ? 'Мониторинг'
                : 'Ожидание'
            }}
          </strong>
        </div>

        <div class="status-card">
          <span class="status-label">
            ЛИГА
          </span>

          <strong>
            {{
              data.league ||
              'FC 25. 3x3'
            }}
          </strong>
        </div>

        <div class="status-card">
          <span class="status-label">
            ГРЯДУЩИХ МАТЧЕЙ
          </span>

          <strong class="status-big">
            {{ matches.length }}
          </strong>
        </div>

        <div class="status-card">
          <span class="status-label">
            ОБНОВЛЕНО
          </span>

          <strong>
            {{ updatedAt }}
          </strong>
        </div>
      </section>


      <!-- ================================================= -->
      <!-- ERROR -->
      <!-- ================================================= -->

      <div
        v-if="error"
        class="error-box"
      >
        <strong>
          Нет данных от автобота
        </strong>

        <span>
          {{ error }}
        </span>
      </div>


      <!-- ================================================= -->
      <!-- LOADING -->
      <!-- ================================================= -->

      <div
        v-else-if="loading"
        class="empty"
      >
        Получаем информацию от автобота...
      </div>


      <template v-else>
        <!-- =============================================== -->
        <!-- NEXT MATCH -->
        <!-- =============================================== -->

        <section class="section">
          <div class="section-title">
            <div>
              <span class="section-label">
                ВЫБРАН БОТОМ
              </span>

              <h2>
                Ближайший матч
              </h2>
            </div>

            <div
              v-if="nextMatch"
              class="countdown"
            >
              <span>
                ДО НАЧАЛА
              </span>

              <strong>
                {{ nextMatch.time || '—' }}
              </strong>
            </div>
          </div>


          <article
            v-if="nextMatch"
            class="active-match"
          >
            <!-- TOP -->

            <div class="active-top">
              <div class="match-id">
                ID МАТЧА
                #{{ nextMatch.match_id || nextMatch.number }}
              </div>

              <div class="waiting-badge">
                ОЖИДАЕТ НАЧАЛА
              </div>
            </div>


            <!-- TEAMS -->

            <div class="active-content">
              <!-- TEAM 1 -->

              <div
                class="active-team"
                :class="{
                  outsider:
                    nextOutsider?.side === 1,
                }"
              >
                <div
                  v-if="nextOutsider?.side === 1"
                  class="outsider-badge"
                >
                  АУТСАЙДЕР
                </div>

                <div class="team-side">
                  КОМАНДА 1
                </div>

                <div class="active-team-name">
                  {{ nextMatch.team1 }}
                </div>

                <div class="active-odd">
                  <span>
                    П1
                  </span>

                  <strong>
                    {{
                      displayOdd(
                        nextMatch.odds_team1,
                      )
                    }}
                  </strong>
                </div>
              </div>


              <!-- DRAW -->

              <div class="active-center">
                <div class="vs">
                  VS
                </div>

                <div class="draw">
                  <span>
                    НИЧЬЯ
                  </span>

                  <strong>
                    {{
                      displayOdd(
                        nextMatch.odds_draw,
                      )
                    }}
                  </strong>
                </div>
              </div>


              <!-- TEAM 2 -->

              <div
                class="active-team"
                :class="{
                  outsider:
                    nextOutsider?.side === 2,
                }"
              >
                <div
                  v-if="nextOutsider?.side === 2"
                  class="outsider-badge"
                >
                  АУТСАЙДЕР
                </div>

                <div class="team-side">
                  КОМАНДА 2
                </div>

                <div class="active-team-name">
                  {{ nextMatch.team2 }}
                </div>

                <div class="active-odd">
                  <span>
                    П2
                  </span>

                  <strong>
                    {{
                      displayOdd(
                        nextMatch.odds_team2,
                      )
                    }}
                  </strong>
                </div>
              </div>
            </div>


            <!-- BOT DECISION -->

            <div class="bot-decision">
              <div class="decision-icon">
                →
              </div>

              <div>
                <span>
                  ВЫБОР АВТОБОТА
                </span>

                <strong v-if="nextOutsider">
                  {{ nextOutsider.team }}
                </strong>

                <strong v-else>
                  Аутсайдер не определён
                </strong>
              </div>

              <div
                v-if="nextOutsider"
                class="decision-odd"
              >
                КФ {{ nextOutsider.odd }}
              </div>
            </div>
          </article>


          <div
            v-else
            class="empty"
          >
            Грядущих матчей сейчас нет.
          </div>
        </section>


        <!-- =============================================== -->
        <!-- QUEUE -->
        <!-- =============================================== -->

        <section class="section">
          <div class="section-title">
            <div>
              <span class="section-label">
                МОНИТОРИНГ
              </span>

              <h2>
                Очередь матчей
              </h2>
            </div>

            <div class="queue-count">
              {{ queueMatches.length }}
            </div>
          </div>


          <div
            v-if="queueMatches.length"
            class="queue"
          >
            <article
              v-for="match in queueMatches"
              :key="
                match.match_id ||
                match.number
              "
              class="queue-card"
            >
              <div class="queue-number">
                <span>
                  MATCH
                </span>

                <strong>
                  {{ match.number }}
                </strong>
              </div>


              <div class="queue-teams">
                <div class="queue-team">
                  <div>
                    {{ match.team1 }}
                  </div>

                  <strong>
                    {{
                      displayOdd(
                        match.odds_team1,
                      )
                    }}
                  </strong>
                </div>

                <div class="queue-draw">
                  <span>
                    X
                  </span>

                  <strong>
                    {{
                      displayOdd(
                        match.odds_draw,
                      )
                    }}
                  </strong>
                </div>

                <div class="queue-team">
                  <div>
                    {{ match.team2 }}
                  </div>

                  <strong>
                    {{
                      displayOdd(
                        match.odds_team2,
                      )
                    }}
                  </strong>
                </div>
              </div>


              <div class="queue-time">
                <span>
                  ДО НАЧАЛА
                </span>

                <strong>
                  {{ match.time || '—' }}
                </strong>
              </div>


              <div class="queue-outsider">
                <span>
                  АУТСАЙДЕР
                </span>

                <strong>
                  {{ getOutsider(match)?.team || 'Не определён' }}
                </strong>
              </div>
            </article>
          </div>


          <div
            v-else
            class="empty-small"
          >
            В очереди больше нет матчей.
          </div>
        </section>
      </template>
    </main>
  </div>
</template>
