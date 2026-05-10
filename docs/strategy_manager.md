# Plan: UI-Based Strategy Manager

## Context
Currently all strategy parameters (cashout targets, triggers, bet limits, stop guards) live in `config.py` and are shared across every bot session. The user wants to:
1. Define named strategies in the web UI (not by editing a file)
2. Save multiple strategies persistently
3. Pick which strategy a session uses at start time

---

## Architecture Overview

```
strategies.json  ←  persisted strategy definitions (server filesystem)
server.py        ←  CRUD endpoints + strategy loading at session start
src/bot.py       ←  accepts strategy dict, uses instance attrs instead of config.*
index.html       ←  strategy manager panel + selector on start
```

---

## Strategy Schema

Each strategy object:
```json
{
  "id": "uuid4-string",
  "name": "My Strategy",
  "panel1_cashout": 6.0,
  "panel2_cashout": 3.0,
  "trigger_mult": 9.0,
  "low_streak_max": 3.0,
  "max_bet_rounds": 4,
  "recovery_profit_target": 5,
  "stop_on_profit": 500,
  "stop_on_loss": -200,
  "bet_amount": 1
}
```

Non-strategy params (SLOW_MO, BROWSER_TIMEOUT, URLs) stay in `config.py` — they're infrastructure, not strategy.

---

## Files to Modify

| File | Change |
|---|---|
| `server.py` | Add strategy CRUD endpoints + `strategies.json` helpers + pass strategy to bot |
| `src/bot.py` | Add `strategy: dict` param to `__init__`, store as instance attrs, replace `config.*` refs |
| `static/index.html` | Strategy manager panel + strategy selector dropdown on start |

`bot.py` (local runner) is **not changed** — it continues reading `config.py` directly.

---

## Step 1 — `strategies.json` persistence helpers (server.py)

Add at top of `server.py`:

```python
import uuid, json
from pathlib import Path

STRATEGIES_FILE = Path("strategies.json")

def _load_strategies() -> list[dict]:
    if not STRATEGIES_FILE.exists():
        return []
    return json.loads(STRATEGIES_FILE.read_text())

def _save_strategies(strategies: list[dict]):
    STRATEGIES_FILE.write_text(json.dumps(strategies, indent=2))

def _default_strategy() -> dict:
    return {
        "id": str(uuid.uuid4()),
        "name": "Default",
        "panel1_cashout": config.PANEL1_CASHOUT,
        "panel2_cashout": config.PANEL2_CASHOUT,
        "trigger_mult": config.TRIGGER_MULT,
        "low_streak_max": config.LOW_STREAK_MAX,
        "max_bet_rounds": config.MAX_BET_ROUNDS,
        "recovery_profit_target": config.RECOVERY_PROFIT_TARGET,
        "stop_on_profit": config.STOP_ON_PROFIT,
        "stop_on_loss": config.STOP_ON_LOSS,
        "bet_amount": config.BET_AMOUNT,
    }
```

On server startup, if `strategies.json` doesn't exist → seed it with one "Default" strategy.

---

## Step 2 — Strategy API endpoints (server.py)

```
GET    /strategies              → list all strategies
POST   /strategies              → create strategy (body: strategy fields + name)
PUT    /strategies/{id}         → update strategy by id
DELETE /strategies/{id}         → delete strategy by id
```

Pydantic model `StrategyModel` mirrors the schema above (all fields optional on PUT).

---

## Step 3 — Link strategy to session (server.py)

Extend `StartRequest`:
```python
class StartRequest(BaseModel):
    username: str
    password: str
    headless: bool = True
    strategy_id: str | None = None   # ← NEW
```

In `POST /sessions/start`: load strategy by `strategy_id` (or use default if None), pass it to bot.

---

## Step 4 — `src/bot.py`: accept strategy dict

In `AviatorBot.__init__`, add `strategy: dict = None` param. Store all strategy params as instance attrs:

```python
s = strategy or {}
self.PANEL1_CASHOUT          = s.get("panel1_cashout",          config.PANEL1_CASHOUT)
self.PANEL2_CASHOUT          = s.get("panel2_cashout",          config.PANEL2_CASHOUT)
self.TRIGGER_MULT            = s.get("trigger_mult",            config.TRIGGER_MULT)
self.LOW_STREAK_MAX          = s.get("low_streak_max",          config.LOW_STREAK_MAX)
self.MAX_BET_ROUNDS          = s.get("max_bet_rounds",          config.MAX_BET_ROUNDS)
self.RECOVERY_PROFIT_TARGET  = s.get("recovery_profit_target",  config.RECOVERY_PROFIT_TARGET)
self.STOP_ON_PROFIT          = s.get("stop_on_profit",          config.STOP_ON_PROFIT)
self.STOP_ON_LOSS            = s.get("stop_on_loss",            config.STOP_ON_LOSS)
self.BET_AMOUNT              = s.get("bet_amount",              config.BET_AMOUNT)
```

Then do a global replace in `src/bot.py` of all `config.PANEL1_CASHOUT` → `self.PANEL1_CASHOUT` etc. (9 replacements across ~40 references).

Also expose active strategy name in `last_event` / status response so the UI can show which strategy is running.

---

## Step 5 — `index.html` UI changes

### Strategy Manager panel (new collapsible section above START BOT)
- Table listing saved strategies (name + key params)
- "+ New Strategy" button → opens inline form with all fields pre-filled with defaults
- Edit / Delete per row
- On save → POST or PUT to API

### Start Bot flow
- Dropdown "Select Strategy" populated from `GET /strategies`
- Default: first strategy in list (or "Default")
- Selected `strategy_id` sent in `POST /sessions/start` body

### Dashboard
- Add one line: `Strategy: [name]` in the stats grid
- Include `strategy_name` in `StatusResponse` from server

---

## Status response update (server.py)

Add `strategy_name: str` field to `StatusResponse`. Store strategy name on the session object when the bot starts.

---

## Verification

1. Start server: `uvicorn server:app --host 0.0.0.0 --port 8000`
2. Open `http://localhost:8000` — check Strategy Manager loads with "Default" strategy
3. Create a second strategy with different values → appears in list
4. Start a bot session using the new strategy → dashboard shows correct strategy name
5. Check `strategies.json` was written to disk
6. Restart server → strategies still present (persistence check)
7. Delete a strategy → removed from list and file
8. `bot.py` (local) still runs unchanged via `python bot.py`




source .venv/bin/activate
uvicorn server:app --host 0.0.0.0 --port 8000
Before it can work live, set these env vars:

MPESA_ENV=sandbox or production
MPESA_CONSUMER_KEY
MPESA_CONSUMER_SECRET
MPESA_SHORTCODE
MPESA_PASSKEY
MPESA_CALLBACK_URL
Use your public FastAPI callback URL for MPESA_CALLBACK_URL, pointing to:

/payments/mpesa/callback