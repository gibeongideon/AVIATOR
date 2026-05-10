# Aviator Bot — Project Guide

## What this project is
Automated betting bot for the SportPesa Aviator game (Spribe).
Uses Playwright to control a real Chrome browser and place bets based on crash history patterns.

## Branch structure
| Branch | Purpose |
|---|---|
| `main` | Stable standalone bot — `bot.py` only, no server |
| `feature/fastapi-server` | Server + web UI + mobile API layer on top of `main` |

---

## File layout (feature/fastapi-server branch)

```
AVIATOR/
├── bot.py                 ← LOCAL runner (standalone, no server needed)
├── config.py              ← Shared config — edit this before running anything
├── server.py              ← FastAPI server (web UI + API for mobile app)
├── src/
│   └── bot.py             ← Server version of the bot (per-session credentials,
│                             stop event, headless flag, session logger)
├── static/
│   └── index.html         ← Web UI served at http://server:8000/
├── requirements.txt       ← Local bot deps (playwright)
├── requirements_server.txt← Server deps (fastapi, uvicorn, playwright)
├── logs/                  ← Log files written here (gitignored)
└── history/               ← CSV round history written here (gitignored)
```

---

## Two bot files — IMPORTANT

There are intentionally **two copies** of the bot:

| File | Used by | Credentials | Stop mechanism |
|---|---|---|---|
| `bot.py` | Local CLI (`python bot.py`) | Reads `config.py` | Ctrl+C |
| `src/bot.py` | FastAPI server | Passed per-request | `request_stop()` async event |

**When you change strategy logic** (triggers, bet sizing, cashout targets, etc.)
you must update **both files**. `src/bot.py` has extra server-only additions
at the top of `AviatorBot.__init__` but the strategy loop is identical.

---

## Running locally (no server)

```bash
# 1. Activate virtualenv
source .venv/bin/activate

# 2. Edit credentials + settings
nano config.py

# 3. Run
python bot.py
```

## Running the server (web UI + API)

```bash
source .venv/bin/activate
uvicorn server:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` in a browser.
Users enter their own SportPesa credentials → each gets an isolated headless bot session.

---

## config.py — key settings

```python
USERNAME        = "..."      # SportPesa phone / username
PASSWORD        = "..."      # Password
PANEL1_CASHOUT  = 6.0        # Panel 1 auto-cashout multiplier
PANEL2_CASHOUT  = 3.0        # Panel 2 auto-cashout multiplier
TRIGGER_MULT    = 9.0        # Bet after a crash above this
LOW_STREAK_MAX  = 3.0        # Also bet after 8 consecutive crashes all ≤ this
MAX_BET_ROUNDS  = 4          # Max rounds per betting burst
RECOVERY_PROFIT_TARGET = 1   # KES profit added to deficit before dividing by odds
STOP_ON_PROFIT  = 500        # Global take-profit (KES)
STOP_ON_LOSS    = -200       # Global stop-loss (KES)
HEADLESS        = False      # True = invisible browser (always True in server mode)
```

---

## Bet sizing formula

```
P1 bet = round((recovery_deficit + RECOVERY_PROFIT_TARGET) / PANEL1_CASHOUT, 2)
```
Example: deficit = 16, target = 1, odds = 6 → bet = (17/6) = **2.83 KES**
P2 always stays at 1 KES (BET_AMOUNT). Only P1 scales.

---

## Strategy logic

1. **Watch mode** — observe crash history, no bets placed
2. **Trigger** — activate betting when:
   - Last crash > `TRIGGER_MULT` (9x), OR
   - All of the last 8 crashes ≤ `LOW_STREAK_MAX` (3x)
3. **Bet mode** — place bets on both panels for up to `MAX_BET_ROUNDS` rounds
   - P1 scales up each round to recover cumulative deficit
   - P2 always bets 1 KES at 3x cashout
4. **Recovery** — deficit tracks total losses; resets only when P1 wins (crash ≥ 6x)
5. **Exit** — return to watch mode after a win or after `MAX_BET_ROUNDS` exhausted

---

## FastAPI endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Web UI (index.html) |
| `POST` | `/sessions/start` | Start bot — body: `{username, password, headless}` → returns `session_id` |
| `POST` | `/sessions/{id}/stop` | Graceful stop after current round |
| `GET` | `/sessions/{id}/status` | Live status — PnL, rounds, state, last event |
| `GET` | `/health` | Active session count |

The mobile Flutter app (branch: `feature/flutter-app`, not yet built)
will use these same endpoints.

---

## Known issues / notes

- `RuntimeError: Event loop is closed` — harmless Playwright GC noise on Python 3.12.
  Only appears in `src/bot.py` (server version). `bot.py` (local) is unaffected.
- The server keeps sessions in memory only — a server restart loses all session state.
- Credentials are sent in plain JSON; use HTTPS (nginx + certbot) in production.
