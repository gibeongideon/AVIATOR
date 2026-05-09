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
import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from bot import AviatorBot

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

# ── Static UI (browser access) ────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", include_in_schema=False)
async def root():
    return FileResponse("static/index.html")

# ── In-memory session store ───────────────────────────────────────────────────
# session_id → { bot, task, state, username, started_at, error }

sessions: dict = {}


# ── Request / response models ─────────────────────────────────────────────────

class StartRequest(BaseModel):
    username: str
    password: str
    headless: bool = True    # default headless on server; user can override


class StartResponse(BaseModel):
    session_id: str
    message: str


class StatusResponse(BaseModel):
    session_id: str
    state: str            # starting | running | stopped | error
    username: str
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

@app.post("/sessions/start", response_model=StartResponse)
async def start_session(req: StartRequest):
    # One active session per username at a time
    for s in sessions.values():
        if s["username"] == req.username and s["state"] in ("starting", "running"):
            raise HTTPException(
                status_code=400,
                detail="A session for this account is already running. Stop it first.",
            )

    session_id = _short_id()
    bot = AviatorBot(
        username=req.username,
        password=req.password,
        session_id=session_id,
        headless=req.headless,
    )

    sessions[session_id] = {
        "username":   req.username,
        "bot":        bot,
        "task":       None,
        "state":      "starting",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "error":      None,
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

    from bot import calc_p1_bet
    return StatusResponse(
        session_id       = session_id,
        state            = s["state"],
        username         = s["username"],
        cumulative_pnl   = round(bot.cumulative_pnl, 2),
        total_rounds     = bot.total_rounds,
        total_wins       = bot.total_wins,
        total_losses     = bot.total_losses,
        recovery_deficit = round(bot.recovery_deficit, 2),
        next_p1_bet      = calc_p1_bet(bot.recovery_deficit),
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
