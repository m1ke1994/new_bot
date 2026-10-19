# AutoBet DEMO

Backend:

```powershell
cd C:\Users\Alex\Desktop\fifa\new_bot
python -m pip install -r requirements.txt
python -m backend.app.main
```

Frontend:

```powershell
cd C:\Users\Alex\Desktop\fifa\new_bot\front\frontend
npm install
npm run dev
```

## Следующий гол: DOM + Canvas Vision

Текущий путь DEMO читает Canvas 2D через перехват команд рисования, а DOM
использует как резервный источник. Перед использованием коэффициентов
сверяется номер следующего гола с текущим счётом. DEMO проверяет выбранный
исход через виртуальный купон и не отправляет денежную ставку.

Persistent Chromium создаётся единственным `BrowserManager` при старте
FastAPI. `POST /api/demo/stop` останавливает только worker; закрыть browser
можно через `POST /api/browser/stop` или штатным завершением backend.

Управление браузером:

```http
GET  /api/browser/state
GET  /api/browser/config-check
POST /api/browser/start
POST /api/browser/stop
```

Диагностика текущего canvas:

```http
POST /api/browser/market-canvas-debug
```

Снимки сохраняются в `backend/diagnostics/canvas/`. Используется только
`canvas.market-grid-canvas__canvas`, полный screenshot браузера не создаётся.
