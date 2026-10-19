# Virtual Forks FC25 — read-only conditional hedge monitor

Base commit: 85b62335e13bf03f2ee2fb05d6e27f87288aea59; branch: feature/fc25-virtual-forks-0500.

## Run locally

From new_bot project root run:
    python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000

From front/frontend run:
    npm ci
    npm run dev

Open http://localhost:5173/forks (or the Vite port shown in the terminal).

## Virtual only

- No real coupons or orders are created; the monitored browser only navigates and reads the next-goal market via existing Canvas 2D instrumentation.
- The same CURRENT next-goal-number row is used for both team outcomes. It does not reuse the already-settled first and second goals.
- Each match is selected from the nearest upcoming match list, then observed at elapsed match time 05:00 through 05:30, inclusive.
- A conditional signal requires both sides unlocked and 1/odd1 + 1/odd2 < 1, with a positive realized profit after rounding and revalidation of score/time.
- The entered budget is a virtual session bankroll. The allocation percent defaults to 10% per pair, adjustable to 1–100%.
- The SQLite ledger at backend/data/virtual_forks.sqlite3 is independent of the main strategy. History persists between sessions; new sessions start a new virtual bankroll.
- A single observed next goal is settled as TEAM_1 or TEAM_2; NO_GOAL requires an explicit finished match status. Ambiguous scores/termination are marked NEEDS_REVIEW rather than arbitrarily settled.
- If the current browser's session is not authenticated, manual login is needed before reading matches.
- The main DEMO and LIVE worker cannot run concurrently with this observer.

## Critical market risk

The bookmaker includes a third outcome — no more goals. Two-sided stakes do not cover it. This is NOT a risk-free arbitrage; if the game ends without another goal, both virtual stakes can lose. Observed odds are NOT proof that any two real bets would be accepted simultaneously.

## Tests

Run from root:
    python -m pytest tests/test_virtual_forks.py -q
    python -m pytest -x -q --tb=short

Run frontend:
    cd front/frontend
    npm ci
    npm run build

The Playwright integration still requires local Windows/browser testing against the actual FC25 site. Do not claim live validation from unit tests alone.

## Endpoints

- POST /api/forks/start  JSON: {"budget": 5000, "allocation_percent": 10}
- POST /api/forks/stop
- GET /api/forks/state
- GET /api/forks/history
- GET /api/forks/logs

Keep the API restricted to localhost unless authentication is added.