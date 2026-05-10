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
import secrets
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import aiosqlite
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from src.bot import AviatorBot, test_credentials
from src.mpesa_fastapi import MpesaConfigError, MpesaService, parse_stk_callback

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Aviator Bot API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://aviator.dafeapp.com",
        "http://localhost:8000",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("aviator-server")

# ── Admin auth ────────────────────────────────────────────────────────────────

_admin_tokens: set[str] = set()   # in-memory; cleared on server restart
_bearer = HTTPBearer(auto_error=False)


def _require_admin(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    if not creds or creds.credentials not in _admin_tokens:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return creds.credentials


@app.on_event("startup")
async def _startup():
    await _init_db()
    log.info("Database ready: %s", DB_FILE)


# ── Strategy persistence ──────────────────────────────────────────────────────

STRATEGIES_FILE = Path("strategies.json")


def _default_strategy() -> dict:
    return {
        "id": str(uuid.uuid4()),
        "name": "Default",
        "trigger_mode": "both",
        "trigger_mult": config.TRIGGER_MULT,
        "low_streak_max": config.LOW_STREAK_MAX,
        "low_streak_rounds": 8,
        "panel1_cashout": config.PANEL1_CASHOUT,
        "panel2_cashout": config.PANEL2_CASHOUT,
        "bet_amount": config.BET_AMOUNT,
        "p2_bet_amount": config.P2_BET_AMOUNT,
        "stop_on_profit": config.STOP_ON_PROFIT,
        "stop_on_loss": config.STOP_ON_LOSS,
        "recovery_enabled": True,
        "recovery_profit_target": config.RECOVERY_PROFIT_TARGET,
        "recovery_scope": "individual",
        "recovery_percentage": 100,
        "recovery_steps": 0,
        "p2_recovery_enabled": False,
        "p2_recovery_profit_target": config.RECOVERY_PROFIT_TARGET,
        "p2_recovery_scope": "individual",
        "p2_recovery_percentage": 100,
        "p2_recovery_steps": 0,
        "max_bet_rounds": config.MAX_BET_ROUNDS,
        "burst_cooldown": 0,
        "stop_on_consecutive_losses": 0,
        "is_paid": False,
        "price_kes": 0,
    }


def _seed_strategies() -> list[dict]:
    """Five ready-to-use scenario presets written on first run."""
    def _s(name, p1, p2, trig, ls_max, ls_rounds, rounds, mode, profit, loss,
           bet=1, p2bet=1, rec=True, p2rec=False, cooldown=0, cons_loss=0,
           rec_scope="individual", rec_pct=100, rec_steps=0,
           p2_scope="individual", p2_pct=100, p2_steps=0,
           paid=False, price=0, days=30):
        return {
            "id":                         str(uuid.uuid4()),
            "name":                       name,
            "trigger_mode":               mode,
            "trigger_mult":               trig,
            "low_streak_max":             ls_max,
            "low_streak_rounds":          ls_rounds,
            "panel1_cashout":             p1,
            "panel2_cashout":             p2,
            "bet_amount":                 bet,
            "p2_bet_amount":              p2bet,
            "stop_on_profit":             profit,
            "stop_on_loss":               loss,
            "recovery_enabled":           rec,
            "recovery_profit_target":     5,
            "recovery_scope":             rec_scope,
            "recovery_percentage":        rec_pct,
            "recovery_steps":             rec_steps,
            "p2_recovery_enabled":        p2rec,
            "p2_recovery_profit_target":  5,
            "p2_recovery_scope":          p2_scope,
            "p2_recovery_percentage":     p2_pct,
            "p2_recovery_steps":          p2_steps,
            "max_bet_rounds":             rounds,
            "burst_cooldown":             cooldown,
            "stop_on_consecutive_losses": cons_loss,
            "is_paid":                    paid,
            "price_kes":                  price,
            "duration_days":              days,
        }
    ai_base = _s("AI Adaptive", p1=6, p2=3, trig=9, ls_max=3, ls_rounds=8, rounds=4,
                 mode="both", profit=500, loss=-200)
    ai_base["strategy_type"] = "ai"
    return [
        _s("Conservative",      p1=3,  p2=2,   trig=7,    ls_max=2,    ls_rounds=10, rounds=2,
           mode="both",      profit=200,  loss=-100, cooldown=3, cons_loss=4),
        _s("Default",           p1=6,  p2=3,   trig=9,    ls_max=3,    ls_rounds=8,  rounds=4,
           mode="both",      profit=500,  loss=-200),
        _s("Aggressive",        p1=10, p2=5,   trig=15,   ls_max=4,    ls_rounds=8,  rounds=6,
           mode="both",      profit=1000, loss=-500, bet=2, p2bet=2, p2rec=True,
           rec_scope="combined",
           paid=True, price=250, days=30),
        _s("Low-Streak Hunter", p1=5,  p2=2.5, trig=9999, ls_max=3,    ls_rounds=12, rounds=4,
           mode="low_only",  profit=300,  loss=-150, cooldown=5, cons_loss=6, paid=True, price=200, days=30),
        _s("High-Crash Sniper", p1=8,  p2=4,   trig=20,   ls_max=9999, ls_rounds=8,  rounds=2,
           mode="high_only", profit=500,  loss=-100, bet=2, p2bet=2, rec=False,
           cooldown=2, cons_loss=2, paid=True, price=300, days=30),
        ai_base,
    ]


def _load_strategies() -> list[dict]:
    if not STRATEGIES_FILE.exists():
        seed = _seed_strategies()
        _save_strategies(seed)
        return seed
    strategies = json.loads(STRATEGIES_FILE.read_text())
    changed = False
    for strategy in strategies:
        if "price_kes" not in strategy:
            strategy["price_kes"] = 0
            changed = True
        if "duration_days" not in strategy:
            strategy["duration_days"] = 30 if strategy.get("is_paid") else 0
            changed = True
        if "p2_bet_amount" not in strategy:
            strategy["p2_bet_amount"] = strategy.get("bet_amount", 1)
            changed = True
        if "p2_recovery_enabled" not in strategy:
            strategy["p2_recovery_enabled"] = False
            changed = True
        if "p2_recovery_profit_target" not in strategy:
            strategy["p2_recovery_profit_target"] = strategy.get("recovery_profit_target", 5)
            changed = True
        if "recovery_scope" not in strategy:
            strategy["recovery_scope"] = "individual"
            changed = True
        if "recovery_percentage" not in strategy:
            strategy["recovery_percentage"] = 100
            changed = True
        if "p2_recovery_scope" not in strategy:
            strategy["p2_recovery_scope"] = "individual"
            changed = True
        if "p2_recovery_percentage" not in strategy:
            strategy["p2_recovery_percentage"] = 100
            changed = True
        if "recovery_steps" not in strategy:
            strategy["recovery_steps"] = 0
            changed = True
        if "p2_recovery_steps" not in strategy:
            strategy["p2_recovery_steps"] = 0
            changed = True
        if "created_by" not in strategy:
            strategy["created_by"] = ""   # existing strategies are admin/global
            changed = True
        if "strategy_type" not in strategy:
            strategy["strategy_type"] = "fixed"
            changed = True
    if changed:
        _save_strategies(strategies)
    return strategies


def _save_strategies(strategies: list[dict]):
    STRATEGIES_FILE.write_text(json.dumps(strategies, indent=2))


# ── Database ──────────────────────────────────────────────────────────────────

DB_FILE = Path("aviator.db")


async def _init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                username   TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                last_login TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                strategy_id TEXT    NOT NULL,
                paid_at     TEXT    NOT NULL,
                expires_at  TEXT,
                notes       TEXT,
                UNIQUE(user_id, strategy_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS mpesa_transactions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                username            TEXT    NOT NULL,
                strategy_id         TEXT    NOT NULL,
                strategy_name       TEXT    NOT NULL,
                amount              REAL    NOT NULL,
                phone_number        TEXT    NOT NULL,
                reference           TEXT    NOT NULL UNIQUE,
                merchant_request_id TEXT,
                checkout_request_id TEXT    UNIQUE,
                status              TEXT    NOT NULL,
                result_code         TEXT,
                result_desc         TEXT,
                receipt_number      TEXT,
                raw_request         TEXT,
                raw_response        TEXT,
                raw_callback        TEXT,
                paid_at             TEXT,
                created_at          TEXT    NOT NULL,
                updated_at          TEXT    NOT NULL
            )
        """)
        await db.commit()


async def _upsert_user(username: str) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            INSERT INTO users (username, created_at, last_login) VALUES (?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET last_login = excluded.last_login
        """, (username, now, now))
        await db.commit()
        cur = await db.execute("SELECT id FROM users WHERE username = ?", (username,))
        row = await cur.fetchone()
        return row[0]


async def _get_user_access(username: str) -> list[dict]:
    """Return active (non-expired) access records for a user, with days_remaining."""
    now = datetime.now()
    now_iso = now.isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("""
            SELECT p.strategy_id, p.expires_at FROM payments p
            JOIN users u ON u.id = p.user_id
            WHERE u.username = ?
              AND (p.expires_at IS NULL OR p.expires_at > ?)
        """, (username, now_iso))
        rows = await cur.fetchall()
    result = []
    for strategy_id, expires_at in rows:
        if expires_at:
            exp_dt = datetime.fromisoformat(expires_at)
            days_remaining = max(0, (exp_dt - now).days)
        else:
            days_remaining = None  # lifetime
        result.append({
            "strategy_id":   strategy_id,
            "expires_at":    expires_at,
            "days_remaining": days_remaining,
        })
    return result


async def _get_unlocked_strategy_ids(username: str) -> list[str]:
    return [r["strategy_id"] for r in await _get_user_access(username)]


async def _user_has_strategy_access(username: str, strategy_id: str) -> bool:
    return strategy_id in await _get_unlocked_strategy_ids(username)


def _calc_expires_at(duration_days: int) -> str | None:
    """Return ISO expires_at string, or None for lifetime (duration_days == 0)."""
    if not duration_days:
        return None
    return (datetime.now() + timedelta(days=duration_days)).isoformat(timespec="seconds")


async def _grant_strategy_access(
    username: str,
    strategy_id: str,
    *,
    notes: str | None = None,
    expires_at: str | None = None,
    duration_days: int = 0,
) -> None:
    if expires_at is None and duration_days:
        expires_at = _calc_expires_at(duration_days)
    user_id = await _upsert_user(username)
    now = datetime.now().isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            INSERT INTO payments (user_id, strategy_id, paid_at, expires_at, notes)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, strategy_id) DO UPDATE
              SET paid_at=excluded.paid_at, expires_at=excluded.expires_at, notes=excluded.notes
        """, (user_id, strategy_id, now, expires_at, notes))
        await db.commit()


def _find_strategy(strategy_id: str) -> dict:
    strategy = next((s for s in _load_strategies() if s["id"] == strategy_id), None)
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return strategy


def _strategy_price(strategy: dict) -> int:
    price = int(round(float(strategy.get("price_kes") or 0)))
    if strategy.get("is_paid") and price <= 0:
        raise HTTPException(
            status_code=400,
            detail=f'Strategy "{strategy["name"]}" is marked paid but has no valid M-Pesa price.',
        )
    return price


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
    name:                       str
    # ── Trigger ───────────────────────────────────────────────────────────────
    trigger_mode:               str   = "both"   # "both" | "high_only" | "low_only"
    trigger_mult:               float = config.TRIGGER_MULT
    low_streak_max:             float = config.LOW_STREAK_MAX
    low_streak_rounds:          int   = 8
    # ── Panels ────────────────────────────────────────────────────────────────
    panel1_cashout:             float = config.PANEL1_CASHOUT
    panel2_cashout:             float = config.PANEL2_CASHOUT
    bet_amount:                 float = config.BET_AMOUNT
    p2_bet_amount:              float = config.P2_BET_AMOUNT
    # ── Session guards ────────────────────────────────────────────────────────
    stop_on_profit:             float = config.STOP_ON_PROFIT
    stop_on_loss:               float = config.STOP_ON_LOSS
    # ── Panel 1 recovery ──────────────────────────────────────────────────────
    recovery_enabled:           bool  = True
    recovery_profit_target:     float = config.RECOVERY_PROFIT_TARGET
    recovery_scope:             str   = "individual"  # "individual" | "combined" | "percentage"
    recovery_percentage:        int   = 100           # % of deficit to recover per P1 win
    recovery_steps:             int   = 0             # rounds to apply % recovery (0 = max_bet_rounds)
    # ── Panel 2 recovery (independent) ───────────────────────────────────────
    p2_recovery_enabled:        bool  = False
    p2_recovery_profit_target:  float = config.RECOVERY_PROFIT_TARGET
    p2_recovery_scope:          str   = "individual"
    p2_recovery_percentage:     int   = 100
    p2_recovery_steps:          int   = 0
    # ── Ownership ─────────────────────────────────────────────────────────────
    created_by:                 str   = ""  # "" = admin/global; username = user-private
    # ── General ───────────────────────────────────────────────────────────────
    max_bet_rounds:             int   = config.MAX_BET_ROUNDS
    burst_cooldown:             int   = 0
    stop_on_consecutive_losses: int   = 0
    is_paid:                    bool  = False
    price_kes:                  float = 0
    duration_days:              int   = 30   # access duration after purchase; 0 = lifetime
    strategy_type:              str   = "fixed"   # "fixed" | "ai"
    ai_history_window:          int   = 10        # rounds of crash history the AI analyzes


class StartRequest(BaseModel):
    username:    str
    password:    str
    headless:    bool = True
    strategy_id: Optional[str] = None
    demo_mode:   bool = True
    auto_logout: bool = True


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
    p2_recovery_deficit: float = 0.0
    next_p2_bet: float = 1.0
    last_event: str
    started_at: str
    error: Optional[str]
    ai_overrides: dict = {}


class MpesaStkPushRequest(BaseModel):
    username: str
    strategy_id: str
    phone_number: str


class GrantPaymentRequest(BaseModel):
    strategy_id:   str
    duration_days: int           = 30   # 0 = lifetime
    notes:         Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _short_id() -> str:
    return uuid.uuid4().hex[:8]


def _get_session(session_id: str) -> dict:
    s = sessions.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    return s


def _get_mpesa_service(request: Request | None = None) -> MpesaService:
    callback_url = None
    if request is not None:
        callback_url = str(request.url_for("mpesa_callback"))
    try:
        return MpesaService.from_env(callback_url=callback_url)
    except MpesaConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def _load_mpesa_transaction_by_checkout(checkout_request_id: str) -> dict | None:
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM mpesa_transactions WHERE checkout_request_id = ?",
            (checkout_request_id,),
        )
        row = await cur.fetchone()
    return dict(row) if row else None


async def _mark_mpesa_transaction(
    checkout_request_id: str,
    *,
    status: str,
    result_code: str | None = None,
    result_desc: str | None = None,
    receipt_number: str | None = None,
    raw_callback: dict | None = None,
    paid_at: str | None = None,
) -> dict | None:
    tx = await _load_mpesa_transaction_by_checkout(checkout_request_id)
    if not tx:
        return None

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            UPDATE mpesa_transactions
               SET status = ?,
                   result_code = ?,
                   result_desc = ?,
                   receipt_number = COALESCE(?, receipt_number),
                   raw_callback = COALESCE(?, raw_callback),
                   paid_at = COALESCE(?, paid_at),
                   updated_at = ?
             WHERE checkout_request_id = ?
        """, (
            status,
            result_code,
            result_desc,
            receipt_number,
            json.dumps(raw_callback) if raw_callback is not None else None,
            paid_at,
            datetime.now().isoformat(timespec="seconds"),
            checkout_request_id,
        ))
        await db.commit()

    tx = await _load_mpesa_transaction_by_checkout(checkout_request_id)
    if tx and status == "paid":
        strategy = next((s for s in _load_strategies() if s["id"] == tx["strategy_id"]), {})
        await _grant_strategy_access(
            tx["username"],
            tx["strategy_id"],
            notes=f"M-Pesa receipt {receipt_number or 'pending'}",
            duration_days=int(strategy.get("duration_days") or 30),
        )
    return tx


# ── Endpoints ─────────────────────────────────────────────────────────────────

# -- Strategy CRUD ------------------------------------------------------------

@app.get("/strategies")
async def list_strategies(user: Optional[str] = None):
    strategies = _load_strategies()
    if user:
        strategies = [s for s in strategies if s.get("created_by", "") in ("", user)]
    return strategies


@app.post("/strategies", status_code=201)
async def create_strategy(body: StrategyModel):
    if body.is_paid and body.price_kes <= 0:
        raise HTTPException(status_code=400, detail="Paid strategies need a price greater than 0 KES.")
    strategies = _load_strategies()
    new = {"id": str(uuid.uuid4()), **body.model_dump()}
    strategies.append(new)
    _save_strategies(strategies)
    return new


@app.put("/strategies/{strategy_id}")
async def update_strategy(strategy_id: str, body: StrategyModel):
    if body.is_paid and body.price_kes <= 0:
        raise HTTPException(status_code=400, detail="Paid strategies need a price greater than 0 KES.")
    strategies = _load_strategies()
    for i, s in enumerate(strategies):
        if s["id"] == strategy_id:
            updated = {"id": strategy_id, **body.model_dump()}
            # Preserve original created_by — prevent ownership change via PUT
            updated["created_by"] = s.get("created_by", body.created_by)
            strategies[i] = updated
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
    access: list[dict] = []
    if result["ok"]:
        await _upsert_user(req.username)
        access = await _get_user_access(req.username)
        log.info("User %s registered/updated. Active access: %d strategies", req.username, len(access))
    return {
        **result,
        "reset_url": "https://www.ke.sportpesa.com/forgot-password",
        "unlocked_strategy_ids": [r["strategy_id"] for r in access],
        "access_map": {r["strategy_id"]: r for r in access},
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

    if strategy.get("is_paid"):
        await _upsert_user(req.username)
        if not await _user_has_strategy_access(req.username, strategy["id"]):
            raise HTTPException(
                status_code=403,
                detail="This paid strategy is locked. Complete M-Pesa payment first.",
            )

    session_id = _short_id()
    bot = AviatorBot(
        username=req.username,
        password=req.password,
        session_id=session_id,
        headless=req.headless,
        strategy=strategy,
        demo_mode=req.demo_mode,
        auto_logout=req.auto_logout,
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


class AiParamsRequest(BaseModel):
    bet_amount:     Optional[float] = None
    p2_bet_amount:  Optional[float] = None
    panel1_cashout: Optional[float] = None
    panel2_cashout: Optional[float] = None


@app.post("/sessions/{session_id}/ai-params")
async def set_session_ai_params(session_id: str, body: AiParamsRequest):
    """
    Manually push parameter overrides to a running AI strategy session.
    Only works for sessions started with strategy_type == "ai".
    Overrides are applied on the next watch round.
    """
    s = _get_session(session_id)
    bot: AviatorBot = s["bot"]
    if getattr(bot, "_strategy_type", "fixed") != "ai":
        raise HTTPException(
            status_code=400,
            detail="This session is not using an AI strategy.",
        )
    params = {k: v for k, v in body.model_dump().items() if v is not None}
    bot.set_ai_params(params)
    log.info("AI params set for session %s: %s", session_id, params)
    return {"message": "AI params updated — applied on next watch round.", "params": params}


@app.get("/sessions/{session_id}/ai-params")
async def get_session_ai_params(session_id: str):
    """Return the current AI parameter overrides for a session."""
    s = _get_session(session_id)
    bot: AviatorBot = s["bot"]
    return {
        "session_id":    session_id,
        "strategy_type": getattr(bot, "_strategy_type", "fixed"),
        "ai_overrides":  getattr(bot, "_ai_overrides", {}),
    }


@app.get("/sessions/{session_id}/status", response_model=StatusResponse)
async def get_status(session_id: str):
    s   = _get_session(session_id)
    bot: AviatorBot = s["bot"]

    return StatusResponse(
        session_id          = session_id,
        state               = s["state"],
        username            = s["username"],
        strategy_name       = s.get("strategy_name", "—"),
        account_balance     = bot.account_balance,
        cumulative_pnl      = round(bot.cumulative_pnl, 2),
        total_rounds        = bot.total_rounds,
        total_wins          = bot.total_wins,
        total_losses        = bot.total_losses,
        recovery_deficit    = round(bot.recovery_deficit, 2),
        next_p1_bet         = bot._p1_bet(),
        p2_recovery_deficit = round(bot.p2_recovery_deficit, 2),
        next_p2_bet         = bot._p2_bet(),
        last_event          = bot.last_event,
        started_at          = s["started_at"],
        error               = s.get("error"),
        ai_overrides        = getattr(bot, "_ai_overrides", {}),
    )


@app.get("/health")
async def health():
    active = sum(1 for s in sessions.values() if s["state"] in ("starting", "running"))
    return {
        "status":          "ok",
        "active_sessions": active,
        "total_sessions":  len(sessions),
    }


# ── M-Pesa Payments ───────────────────────────────────────────────────────────

@app.get("/payments/mpesa/config")
async def mpesa_config(request: Request):
    try:
        service = _get_mpesa_service(request)
    except HTTPException as exc:
        return {"enabled": False, "detail": exc.detail}
    return {
        "enabled": True,
        "env": service.env,
        "shortcode": service.short_code,
    }


@app.post("/payments/mpesa/initiate")
async def initiate_mpesa_payment(body: MpesaStkPushRequest, request: Request):
    strategy = _find_strategy(body.strategy_id)
    if not strategy.get("is_paid"):
        raise HTTPException(status_code=400, detail="This strategy does not require payment.")

    amount = _strategy_price(strategy)
    if await _user_has_strategy_access(body.username, body.strategy_id):
        return {
            "status": "already_unlocked",
            "message": "This strategy is already unlocked for the user.",
            "strategy_id": body.strategy_id,
        }

    user_id = await _upsert_user(body.username)
    service = _get_mpesa_service(request)
    reference = f"AV{body.strategy_id[:4].upper()}{uuid.uuid4().hex[:6].upper()}"

    try:
        async with httpx.AsyncClient(timeout=35.0) as client:
            result = await service.stk_push(
                client,
                phone_number=body.phone_number,
                amount=amount,
                reference=reference,
                description=f"{strategy['name']} bot access",
            )
    except MpesaConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        try:
            payload = exc.response.json()
            detail = payload.get("errorMessage") or payload.get("ResponseDescription") or detail
        except ValueError:
            pass
        raise HTTPException(status_code=502, detail=f"M-Pesa request failed: {detail}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Unable to reach Safaricom: {exc}") from exc

    response_data = result["response"]
    merchant_request_id = response_data.get("MerchantRequestID")
    checkout_request_id = response_data.get("CheckoutRequestID")
    if not checkout_request_id:
        raise HTTPException(status_code=502, detail="M-Pesa did not return a CheckoutRequestID.")

    now = datetime.now().isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            INSERT INTO mpesa_transactions (
                user_id, username, strategy_id, strategy_name, amount, phone_number, reference,
                merchant_request_id, checkout_request_id, status, result_code, result_desc,
                raw_request, raw_response, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            body.username,
            body.strategy_id,
            strategy["name"],
            amount,
            result["phone_number"],
            reference,
            merchant_request_id,
            checkout_request_id,
            "pending",
            str(response_data.get("ResponseCode", "")),
            response_data.get("ResponseDescription") or response_data.get("CustomerMessage"),
            json.dumps(result["request"]),
            json.dumps(response_data),
            now,
            now,
        ))
        await db.commit()

    return {
        "status": "pending",
        "checkout_request_id": checkout_request_id,
        "merchant_request_id": merchant_request_id,
        "customer_message": response_data.get("CustomerMessage"),
        "amount": amount,
        "phone_number": result["phone_number"],
        "strategy_id": body.strategy_id,
        "strategy_name": strategy["name"],
    }


@app.post("/payments/mpesa/callback", name="mpesa_callback")
async def mpesa_callback(payload: dict):
    parsed = parse_stk_callback(payload)
    checkout_request_id = parsed.get("checkout_request_id")
    if not checkout_request_id:
        return {"ResultCode": 0, "ResultDesc": "Accepted"}

    result_code = parsed.get("result_code")
    status = "paid" if result_code == "0" else "failed"
    paid_at = datetime.now().isoformat(timespec="seconds") if status == "paid" else None
    updated = await _mark_mpesa_transaction(
        checkout_request_id,
        status=status,
        result_code=result_code,
        result_desc=parsed.get("result_desc"),
        receipt_number=parsed.get("receipt_number"),
        raw_callback=payload,
        paid_at=paid_at,
    )
    if updated:
        log.info(
            "M-Pesa callback processed: checkout=%s status=%s user=%s strategy=%s",
            checkout_request_id,
            status,
            updated["username"],
            updated["strategy_id"],
        )
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


@app.get("/payments/mpesa/status/{checkout_request_id}")
async def get_mpesa_status(checkout_request_id: str, request: Request):
    tx = await _load_mpesa_transaction_by_checkout(checkout_request_id)
    if not tx:
        raise HTTPException(status_code=404, detail="M-Pesa transaction not found")

    if tx["status"] == "pending":
        service = _get_mpesa_service(request)
        try:
            async with httpx.AsyncClient(timeout=35.0) as client:
                response_data = await service.stk_push_query(
                    client,
                    checkout_request_id=checkout_request_id,
                )
        except httpx.HTTPError:
            response_data = None
        else:
            result_code = str(response_data.get("ResultCode", ""))
            if result_code == "0":
                tx = await _mark_mpesa_transaction(
                    checkout_request_id,
                    status="paid",
                    result_code=result_code,
                    result_desc=response_data.get("ResultDesc"),
                    raw_callback={"query": response_data},
                    paid_at=datetime.now().isoformat(timespec="seconds"),
                ) or tx
            elif result_code:
                tx = await _mark_mpesa_transaction(
                    checkout_request_id,
                    status="failed",
                    result_code=result_code,
                    result_desc=response_data.get("ResultDesc"),
                    raw_callback={"query": response_data},
                ) or tx
            else:
                tx = await _load_mpesa_transaction_by_checkout(checkout_request_id) or tx

    return {
        "checkout_request_id": tx["checkout_request_id"],
        "merchant_request_id": tx["merchant_request_id"],
        "status": tx["status"],
        "result_code": tx["result_code"],
        "result_desc": tx["result_desc"],
        "receipt_number": tx["receipt_number"],
        "strategy_id": tx["strategy_id"],
        "strategy_name": tx["strategy_name"],
        "amount": tx["amount"],
        "phone_number": tx["phone_number"],
        "paid_at": tx["paid_at"],
    }


# ── User & Payment Management ─────────────────────────────────────────────────

@app.get("/users")
async def list_users(_: str = Depends(_require_admin)):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT id, username, created_at, last_login FROM users ORDER BY last_login DESC"
        )
        rows = await cur.fetchall()
    return [
        {"id": r[0], "username": r[1], "created_at": r[2], "last_login": r[3]}
        for r in rows
    ]


@app.get("/users/{username}/payments")
async def get_user_payments(username: str, _: str = Depends(_require_admin)):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT id FROM users WHERE username = ?", (username,))
        user = await cur.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        cur = await db.execute("""
            SELECT p.id, p.strategy_id, p.paid_at, p.expires_at, p.notes
            FROM payments p WHERE p.user_id = ?
        """, (user[0],))
        rows = await cur.fetchall()
    return [
        {"id": r[0], "strategy_id": r[1], "paid_at": r[2], "expires_at": r[3], "notes": r[4]}
        for r in rows
    ]

@app.post("/users/{username}/payments", status_code=201)
async def grant_payment(username: str, body: GrantPaymentRequest, _: str = Depends(_require_admin)):
    """Grant a user access to a paid strategy."""
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT 1 FROM users WHERE username = ?", (username,))
        user = await cur.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
    expires_at = _calc_expires_at(body.duration_days)
    await _grant_strategy_access(
        username,
        body.strategy_id,
        notes=body.notes,
        expires_at=expires_at,
    )
    exp_label = f"{body.duration_days} days" if body.duration_days else "lifetime"
    log.info("Access granted: user=%s strategy=%s duration=%s expires=%s",
             username, body.strategy_id, exp_label, expires_at)
    return {
        "message":   "Access granted",
        "username":  username,
        "strategy_id": body.strategy_id,
        "expires_at":  expires_at,
        "duration":    exp_label,
    }


@app.delete("/users/{username}/payments/{strategy_id}", status_code=204)
async def revoke_payment(username: str, strategy_id: str, _: str = Depends(_require_admin)):
    """Revoke a user's access to a paid strategy."""
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT id FROM users WHERE username = ?", (username,))
        user = await cur.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        await db.execute(
            "DELETE FROM payments WHERE user_id = ? AND strategy_id = ?",
            (user[0], strategy_id)
        )
        await db.commit()
    log.info("Access revoked: user=%s strategy=%s", username, strategy_id)


# ── Admin endpoints ───────────────────────────────────────────────────────────

class AdminLoginRequest(BaseModel):
    password: str


@app.post("/admin/login")
async def admin_login(body: AdminLoginRequest):
    if not secrets.compare_digest(body.password, config.ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="Wrong password.")
    token = secrets.token_hex(32)
    _admin_tokens.add(token)
    log.info("Admin login successful — token issued")
    return {"token": token}


@app.post("/admin/logout")
async def admin_logout(token: str = Depends(_require_admin)):
    _admin_tokens.discard(token)
    return {"message": "Logged out"}


@app.get("/admin/stats")
async def admin_stats(_: str = Depends(_require_admin)):
    async with aiosqlite.connect(DB_FILE) as db:
        (total_users,)  = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())
        (total_grants,) = (await (await db.execute("SELECT COUNT(*) FROM payments")).fetchone())
        row = await (await db.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM mpesa_transactions WHERE status='paid'"
        )).fetchone()
        paid_txns, revenue = row
        (pending_txns,) = (await (await db.execute(
            "SELECT COUNT(*) FROM mpesa_transactions WHERE status='pending'"
        )).fetchone())
    return {
        "total_users":    total_users,
        "total_grants":   total_grants,
        "paid_txns":      paid_txns,
        "revenue_kes":    round(revenue, 2),
        "pending_txns":   pending_txns,
    }


@app.get("/admin/transactions")
async def admin_transactions(_: str = Depends(_require_admin)):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("""
            SELECT
                t.id, t.username, t.strategy_name, t.amount,
                t.phone_number, t.status, t.receipt_number,
                t.created_at, t.paid_at, t.result_desc,
                t.checkout_request_id
            FROM mpesa_transactions t
            ORDER BY t.created_at DESC
        """)
        rows = await cur.fetchall()
    return [
        {
            "id":                  r[0],
            "username":            r[1],
            "strategy_name":       r[2],
            "amount":              r[3],
            "phone_number":        r[4],
            "status":              r[5],
            "receipt_number":      r[6],
            "created_at":          r[7],
            "paid_at":             r[8],
            "result_desc":         r[9],
            "checkout_request_id": r[10],
        }
        for r in rows
    ]


@app.get("/admin/users-with-access")
async def admin_users_with_access(_: str = Depends(_require_admin)):
    """Users list with their unlocked strategy names."""
    strategies = {s["id"]: s["name"] for s in _load_strategies()}
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT id, username, created_at, last_login FROM users ORDER BY last_login DESC"
        )
        users = await cur.fetchall()
        result = []
        for u in users:
            uid, uname, created, last = u
            pcur = await db.execute(
                "SELECT strategy_id, paid_at, expires_at, notes FROM payments WHERE user_id = ?",
                (uid,)
            )
            payments = await pcur.fetchall()
            result.append({
                "username":   uname,
                "created_at": created,
                "last_login": last,
                "access": [
                    {
                        "strategy_id":   p[0],
                        "strategy_name": strategies.get(p[0], p[0]),
                        "paid_at":       p[1],
                        "expires_at":    p[2],
                        "notes":         p[3],
                        "days_remaining": (
                            None if not p[2]
                            else max(0, (datetime.fromisoformat(p[2]) - datetime.now()).days)
                        ),
                    }
                    for p in payments
                ],
            })
    return result


@app.get("/admin", include_in_schema=False)
async def admin_page():
    return FileResponse("static/admin.html")
