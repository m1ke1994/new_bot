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

## Следующий гол: DOM + Canvas Vision

Рабочий reader сначала использует DOM, если исходы рынка доступны как обычные
элементы. Когда коэффициенты отрисованы в Canvas, автоматически включается
машинное зрение: OpenCV + `RapidOCR` (ONNX Runtime), с `pytesseract` как
fallback. Перед использованием коэффициентов проверяются стабильность двух
чтений и номер следующего гола относительно текущего счёта. В LIVE координаты
OCR переводятся в CSS-координаты Canvas перед открытием нужного исхода.

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
