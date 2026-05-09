"""
Aviator Bot — FastAPI Server

Manages per-user bot sessions. Each session runs the full AviatorBot
in a background asyncio task inside this process.

Endpoints:
  POST /sessions/start          — start a bot for a user
  POST /sessions/{id}/stop      — request graceful stop
  GET  /sessions/{id}/status    — poll live status
  GET  /health                  — server health + active session count

Run:
  uvicorn server:app --host 0.0.0.0 --port 8000
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from src.bot import AviatorBot, test_credentials

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Aviator Bot API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("aviator-server")

# ── Strategy persistence ──────────────────────────────────────────────────────

STRATEGIES_FILE = Path("strategies.json")


def _seed_strategies() -> list[dict]:
    """Five ready-to-use scenario presets written on first run."""
    def _s(name, p1, p2, trig, ls_max, ls_rounds, rounds, mode, profit, loss, bet=1):
        return {
            "id":                     str(uuid.uuid4()),
            "name":                   name,
            "panel1_cashout":         p1,
            "panel2_cashout":         p2,
            "trigger_mult":           trig,
            "low_streak_max":         ls_max,
            "low_streak_rounds":      ls_rounds,
            "max_bet_rounds":         rounds,
            "trigger_mode":           mode,
            "recovery_profit_target": 5,
            "stop_on_profit":         profit,
            "stop_on_loss":           loss,
            "bet_amount":             bet,
        }
    return [
        _s("Conservative",      p1=3,  p2=2,   trig=7,    ls_max=2,   ls_rounds=10, rounds=2, mode="both",       profit=200,  loss=-100),
        _s("Default",           p1=6,  p2=3,   trig=9,    ls_max=3,   ls_rounds=8,  rounds=4, mode="both",       profit=500,  loss=-200),
        _s("Aggressive",        p1=10, p2=5,   trig=15,   ls_max=4,   ls_rounds=8,  rounds=6, mode="both",       profit=1000, loss=-500, bet=2),
        _s("Low-Streak Hunter", p1=5,  p2=2.5, trig=9999, ls_max=3,   ls_rounds=12, rounds=4, mode="low_only",   profit=300,  loss=-150),
        _s("High-Crash Sniper", p1=8,  p2=4,   trig=20,   ls_max=9999,ls_rounds=8,  rounds=2, mode="high_only",  profit=500,  loss=-100),
    ]


def _load_strategies() -> list[dict]:
    if not STRATEGIES_FILE.exists():
        seed = _seed_strategies()
        _save_strategies(seed)
        return seed
    return json.loads(STRATEGIES_FILE.read_text())


def _save_strategies(strategies: list[dict]):
    STRATEGIES_FILE.write_text(json.dumps(strategies, indent=2))


# ── Static UI (browser access) ────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", include_in_schema=False)
async def root():
    return FileResponse("static/index.html")

# ── In-memory session store ───────────────────────────────────────────────────
# session_id → { bot, task, state, username, started_at, error }

sessions: dict = {}


# ── Request / response models ─────────────────────────────────────────────────

class StrategyModel(BaseModel):
    name:                   str
    panel1_cashout:         float = config.PANEL1_CASHOUT
    panel2_cashout:         float = config.PANEL2_CASHOUT
    trigger_mult:           float = config.TRIGGER_MULT
    low_streak_max:         float = config.LOW_STREAK_MAX
    low_streak_rounds:      int   = 8
    max_bet_rounds:         int   = config.MAX_BET_ROUNDS
    recovery_profit_target: float = config.RECOVERY_PROFIT_TARGET
    stop_on_profit:         float = config.STOP_ON_PROFIT
    stop_on_loss:           float = config.STOP_ON_LOSS
    bet_amount:             float = config.BET_AMOUNT
    trigger_mode:           str   = "both"   # "both" | "high_only" | "low_only"


class StartRequest(BaseModel):
    username:    str
    password:    str
    headless:    bool = True
    strategy_id: Optional[str] = None


class StartResponse(BaseModel):
    session_id: str
    message: str


class StatusResponse(BaseModel):
    session_id: str
    state: str            # starting | running | stopped | error
    username: str
    strategy_name: str
    account_balance: str
    cumulative_pnl: float
    total_rounds: int
    total_wins: int
    total_losses: int
    recovery_deficit: float
    next_p1_bet: float
    last_event: str
    started_at: str
    error: Optional[str]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _short_id() -> str:
    return uuid.uuid4().hex[:8]


def _get_session(session_id: str) -> dict:
    s = sessions.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    return s


# ── Endpoints ─────────────────────────────────────────────────────────────────

# -- Strategy CRUD ------------------------------------------------------------

@app.get("/strategies")
async def list_strategies():
    return _load_strategies()


@app.post("/strategies", status_code=201)
async def create_strategy(body: StrategyModel):
    strategies = _load_strategies()
    new = {"id": str(uuid.uuid4()), **body.model_dump()}
    strategies.append(new)
    _save_strategies(strategies)
    return new


@app.put("/strategies/{strategy_id}")
async def update_strategy(strategy_id: str, body: StrategyModel):
    strategies = _load_strategies()
    for i, s in enumerate(strategies):
        if s["id"] == strategy_id:
            strategies[i] = {"id": strategy_id, **body.model_dump()}
            _save_strategies(strategies)
            return strategies[i]
    raise HTTPException(status_code=404, detail="Strategy not found")


@app.delete("/strategies/{strategy_id}", status_code=204)
async def delete_strategy(strategy_id: str):
    strategies = _load_strategies()
    updated = [s for s in strategies if s["id"] != strategy_id]
    if len(updated) == len(strategies):
        raise HTTPException(status_code=404, detail="Strategy not found")
    _save_strategies(updated)


# -- Auth + Sessions ----------------------------------------------------------

class TestRequest(BaseModel):
    username: str
    password: str
    headless: bool = True


@app.post("/auth/test")
async def test_login(req: TestRequest):
    """Test SportPesa credentials without starting a full session."""
    log.info("Credential test for user %s", req.username)
    result = await test_credentials(req.username, req.password, headless=req.headless)
    return {
        **result,
        "reset_url": "https://www.ke.sportpesa.com/forgot-password",
    }


@app.post("/sessions/start", response_model=StartResponse)
async def start_session(req: StartRequest):
    # One active session per username at a time
    for s in sessions.values():
        if s["username"] == req.username and s["state"] in ("starting", "running"):
            raise HTTPException(
                status_code=400,
                detail="A session for this account is already running. Stop it first.",
            )

    # Resolve strategy
    strategies = _load_strategies()
    if req.strategy_id:
        strategy = next((s for s in strategies if s["id"] == req.strategy_id), None)
        if strategy is None:
            raise HTTPException(status_code=404, detail="Strategy not found")
    else:
        strategy = strategies[0] if strategies else _default_strategy()

    session_id = _short_id()
    bot = AviatorBot(
        username=req.username,
        password=req.password,
        session_id=session_id,
        headless=req.headless,
        strategy=strategy,
    )

    sessions[session_id] = {
        "username":      req.username,
        "strategy_name": strategy["name"],
        "bot":           bot,
        "task":          None,
        "state":         "starting",
        "started_at":    datetime.now().isoformat(timespec="seconds"),
        "error":         None,
    }

    async def _run():
        try:
            sessions[session_id]["state"] = "running"
            await bot.run()
            sessions[session_id]["state"] = "stopped"
        except Exception as exc:
            log.exception("Session %s crashed: %s", session_id, exc)
            sessions[session_id]["state"] = "error"
            sessions[session_id]["error"] = str(exc)

    task = asyncio.create_task(_run())
    sessions[session_id]["task"] = task

    log.info("Session %s started for user %s", session_id, req.username)
    return StartResponse(
        session_id=session_id,
        message="Bot is starting. Use the session_id to check status.",
    )


@app.post("/sessions/{session_id}/stop")
async def stop_session(session_id: str):
    s = _get_session(session_id)
    if s["state"] not in ("starting", "running"):
        raise HTTPException(
            status_code=400,
            detail=f"Session is already {s['state']}",
        )
    s["bot"].request_stop()
    log.info("Stop requested for session %s", session_id)
    return {"message": "Stop requested. Bot will exit after the current round."}


@app.get("/sessions/{session_id}/status", response_model=StatusResponse)
async def get_status(session_id: str):
    s   = _get_session(session_id)
    bot: AviatorBot = s["bot"]

    return StatusResponse(
        session_id       = session_id,
        state            = s["state"],
        username         = s["username"],
        strategy_name    = s.get("strategy_name", "—"),
        account_balance  = bot.account_balance,
        cumulative_pnl   = round(bot.cumulative_pnl, 2),
        total_rounds     = bot.total_rounds,
        total_wins       = bot.total_wins,
        total_losses     = bot.total_losses,
        recovery_deficit = round(bot.recovery_deficit, 2),
        next_p1_bet      = bot._p1_bet(bot.recovery_deficit),
        last_event       = bot.last_event,
        started_at       = s["started_at"],
        error            = s.get("error"),
    )


@app.get("/health")
async def health():
    active = sum(1 for s in sessions.values() if s["state"] in ("starting", "running"))
    return {
        "status":          "ok",
        "active_sessions": active,
        "total_sessions":  len(sessions),
    }
