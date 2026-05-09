# Aviator Bot

An automated betting bot for the **SportPesa Aviator** game (powered by Spribe). It uses Playwright to drive a real Chrome browser, reads the crash history from the game iframe, and places bets on two independent panels based on configurable trigger conditions and recovery strategies.

---

## What it does

The bot watches every Aviator round without betting. When a configured **trigger condition** fires — a high crash multiplier or a long streak of low crashes — it enters **bet mode** and places bets on both panels for up to N rounds. Each panel has its own cashout target and independent recovery logic. After a win or when all rounds are exhausted, it returns to watch mode.

### Core loop

```text
WATCH → TRIGGER → BET (up to MAX_BET_ROUNDS) → WIN or EXHAUST → WATCH
```

### Trigger conditions (configurable per strategy)

| Mode | Fires when |
|---|---|
| High crash | Last crash multiplier exceeded the trigger threshold (e.g. > 9×) |
| Low streak | All of the last N rounds crashed at or below a low threshold (e.g. ≤ 3× for 8 rounds) |
| Both | Either condition above |

### Bet sizing — Panel 1 (recovery engine)

```
P1 bet = (deficit + RECOVERY_PROFIT_TARGET) / PANEL1_CASHOUT
```

Panel 1 scales up each round to recover the cumulative deficit from previous losses. Panel 2 can be set to flat or independent recovery.

### Recovery scopes

| Scope | Behaviour |
|---|---|
| Individual | Each panel only recovers its own losses |
| Combined | Panel 1 covers both panels' losses together |
| Percentage | Each win recovers X% of total deficit; last step recovers 100%. Steps persist across bursts. |

### Safety guards

- **Stop on Profit / Stop on Loss** — global session limits in KES
- **Burst cooldown** — skip N watch rounds after each burst
- **Stop on consecutive losses** — halt session after N losing rounds in a row

---

## Two modes of use

### 1. Local CLI (standalone)

Runs a single bot session in your terminal. Credentials and settings come from `config.py`.

```bash
source .venv/bin/activate
# Edit config.py with your credentials and preferred settings
python bot.py
```

### 2. Web server (multi-user, browser UI)

A FastAPI server serves a web dashboard. Multiple users can log in with their own SportPesa credentials and run isolated bot sessions simultaneously.

```bash
source .venv/bin/activate
uvicorn server:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` in a browser. Each user:
1. Enters their SportPesa phone number and password
2. Selects or creates a betting strategy
3. Clicks **START BOT** — a headless Chrome session starts in the background
4. Monitors live P&L, round count, and last event from the dashboard

---

## Session options

| Option | Default | Description |
|---|---|---|
| Run headless | On | Chrome runs invisibly in the background |
| Demo mode | On | Dismisses deposit popups and clicks Demo in Spribe (no real money) |
| Stay logged in | Off | When Off, bot logs out of SportPesa when it stops |

---

## Strategies

Strategies are stored in `strategies.json` and managed from the dashboard.

| Field | Description |
|---|---|
| Trigger mode | high_only / low_only / both |
| Panel 1 / Panel 2 cashout | Auto-cashout multipliers |
| Bet amounts | Base bet per panel (KES) |
| Recovery scope | individual / combined / percentage |
| Recovery % | For percentage scope — % of deficit to recover per step |
| Recovery steps | Number of rounds in one percentage cycle (0 = use max bet rounds; persists across bursts) |
| Max bet rounds | Rounds per burst before returning to watch mode |
| Burst cooldown | Watch rounds to skip after each burst |
| Stop on consecutive losses | Halt after N losing rounds |
| Stop on profit / loss | Global KES limits |

### Ownership

- **Global strategies** (created by admin) — visible to all users, read-only for non-admins
- **User strategies** — created from the dashboard, visible and editable only by that user

### Pre-built presets

Five strategies ship on first run:

| Name | Type | Character |
|---|---|---|
| Conservative | Free | Small bets, tight limits, 2-round bursts |
| Default | Free | Balanced — 6× P1 cashout, 4-round bursts |
| Aggressive | Paid | High trigger, both panels recovering, 6-round bursts |
| Low-Streak Hunter | Paid | Fires only on low streaks, patient style |
| High-Crash Sniper | Paid | Fires after very high crashes, flat betting, no recovery |

---

## File layout

```
AVIATOR/
├── bot.py                  ← Local CLI runner (reads config.py)
├── config.py               ← Settings for local bot
├── server.py               ← FastAPI server (web UI + REST API)
├── src/
│   └── bot.py              ← Server bot (per-session credentials, stop event)
├── static/
│   ├── index.html          ← Web dashboard
│   ├── help.html           ← Strategy guide
│   ├── how-it-works.html   ← Detailed mechanics documentation
│   ├── admin.html          ← Admin panel (strategy + user management)
│   └── img/                ← Screenshot assets for help page
├── requirements.txt        ← Local bot dependencies
├── requirements_server.txt ← Server dependencies
├── logs/                   ← Session log files (gitignored)
├── history/                ← Per-session CSV round history (gitignored)
└── strategies.json         ← Persisted strategy definitions (gitignored)
```

> **Two bot files**: `bot.py` (local) and `src/bot.py` (server) are kept in sync. When changing strategy logic, update both.

---

## Key API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Web dashboard |
| `POST` | `/auth/test` | Verify SportPesa credentials |
| `POST` | `/sessions/start` | Start a bot session |
| `POST` | `/sessions/{id}/stop` | Stop a session gracefully |
| `GET` | `/sessions/{id}/status` | Live status — P&L, rounds, state |
| `GET` | `/strategies?user=X` | List strategies visible to user X |
| `POST` | `/strategies` | Create a strategy |
| `PUT` | `/strategies/{id}` | Update a strategy |
| `DELETE` | `/strategies/{id}` | Delete a strategy |
| `POST` | `/payments/mpesa/initiate` | Initiate M-Pesa STK push for paid strategy |
| `GET` | `/admin` | Admin panel |

---

## Requirements

- Python 3.11+
- Playwright (Chromium)
- FastAPI + Uvicorn (server mode)
- A SportPesa account (Kenya)

```bash
pip install -r requirements_server.txt
playwright install chromium
```

---

## Notes

- Credentials are sent as plain JSON — run behind HTTPS (nginx + certbot) in production.
- The server holds sessions in memory; a restart clears all active sessions.
- `RuntimeError: Event loop is closed` in logs is harmless Playwright GC noise on Python 3.12.
- Demo mode reads balance from the Spribe game iframe instead of the SportPesa header.
