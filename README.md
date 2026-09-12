# AutoBet DEMO

Backend:

```powershell
cd C:\Users\Alex\Desktop\new_bot
python -m pip install -r requirements.txt
python -m backend.app.main
```

Frontend:

```powershell
cd C:\Users\Alex\Desktop\new_bot\front\frontend
npm run dev
```

## Вилки настольного тенниса (DEMO)

Стратегия работает с рынком «Партия 2», фиксирует первое плечо на фаворита и
затем отслеживает только противоположного игрока. Минимальный математический
запас вилки настраивается переменной окружения
`TABLE_TENNIS_MIN_ARB_PERCENT` (по умолчанию `0.5`). Частота чтения DOM остаётся
в существующей настройке `TABLE_TENNIS_ODDS_POLL_INTERVAL`. Ставки остаются
виртуальными: после завершения второй партии существующий scoreboard parser
определяет победителя, освобождает резерв и переносит рассчитанный DEMO-баланс
в следующую серию.

## Canvas OCR

Основной движок — singleton `RapidOCR` (ONNX Runtime), fallback —
`pytesseract`. Tesseract ищется через `TESSERACT_CMD`, `PATH` и стандартные
Windows-каталоги. Отсутствие Tesseract не мешает работе RapidOCR.

Persistent Chromium создаётся единственным `BrowserManager` при старте
FastAPI. `POST /api/demo/stop` останавливает только worker; закрыть browser
можно через `POST /api/browser/stop` или штатным завершением backend.

Управление браузером:

```http
GET  http://127.0.0.1:8000/api/browser/state
POST http://127.0.0.1:8000/api/browser/start
POST http://127.0.0.1:8000/api/browser/stop
```

Диагностика текущего canvas:

```http
POST http://127.0.0.1:8000/api/browser/market-canvas-debug
```

Снимки сохраняются в `backend/diagnostics/canvas/`. Используется только
`canvas.market-grid-canvas__canvas`, полный screenshot браузера не создаётся.
