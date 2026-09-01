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
