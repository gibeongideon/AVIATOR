"""
SportPesa Aviator Strategy Bot — Playwright (Python)

Strategy:
  - Both panels always bet 1 KES
  - Panel 1 auto-cashout at 6x, Panel 2 at 3x (set once in the UI)
  - Watch crash history; when the last crash > 9x → activate betting mode
  - Bet up to 4 rounds trying to recover (net positive P&L for that session)
  - Once recovered → return to watch mode
  - After 4 rounds (not recovered) → take the loss, return to watch mode
  - Global stop-loss / take-profit guards for the entire session

Run:  python bot.py
"""

import asyncio
import csv
import logging
import os
import sys
from datetime import datetime
from typing import Optional

from playwright.async_api import (
    async_playwright, Page, Browser, BrowserContext,
    TimeoutError as PWTimeout,
)

import config
from . import ai_strategy

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join("logs", f"aviator_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")),
    ],
)
log = logging.getLogger("aviator-bot")

# ── Confirmed selectors (from inspector.py 2026-05-09) ────────────────────────
SEL = {
    # Login
    "login_user":   'input[name="user"]',
    "login_pass":   'input[name="password"]',
    "login_btn":    '[data-testid="login-form-submit-button"]',
    # Main page
    "cookie_accept": 'button.btn-primary',
    # Bet amount inputs (both panels, placeholder="1")
    "bet_inputs":   'input[placeholder="1"]',
    # Auto Cash Out value input — lives inside .cashout-spinner-wrapper, has NO placeholder attr
    "cashout_input_in_spinner": '.cashout-spinner-wrapper input, .cashout-spinner input',
    # Auto Cash Out toggle switch (div.input-switch.off inside .cash-out-switcher)
    "cashout_toggle_off": '.cash-out-switcher .input-switch.off, .cashout-block .input-switch.off',
    # Green BET button — both panels use this class
    "bet_btn":      'button.btn-success.bet',
    # Crash history bar (newest crash is first line)
    "history":      'div.result-history',
}


# ── Low-level helpers ─────────────────────────────────────────────────────────

async def set_input(inp, value):
    """
    Set a value in an Angular reactive-form input.
    Angular does NOT react to programmatic DOM value changes — it only listens to
    real keyboard events.  triple_click selects all existing text, then type()
    sends actual keystrokes that Angular's (input) handler picks up.
    """
    await inp.click(click_count=3)   # select all existing text
    await asyncio.sleep(0.05)
    await inp.type(str(value), delay=60)   # real keystrokes → Angular model updates
    await inp.press("Tab")                 # blur → triggers validators
    await asyncio.sleep(0.15)


async def get_crash_history(frame) -> list[float]:
    """
    Read crash history from .result-history.
    Returns list of multipliers, newest first.
    """
    try:
        el = await frame.query_selector(SEL["history"])
        if not el:
            return []
        raw = await el.inner_text()
        result = []
        for token in raw.strip().split():
            token = token.replace("x", "").replace(",", ".").strip()
            try:
                result.append(float(token))
            except ValueError:
                pass
        return result
    except Exception:
        return []


async def wait_for_bet_phase(frame, timeout_s: int = 120) -> bool:
    """Wait until the green BET button is visible (= betting phase open)."""
    for _ in range(timeout_s * 4):
        btns = await frame.query_selector_all(SEL["bet_btn"])
        if btns:
            return True
        await asyncio.sleep(0.25)
    return False


async def wait_for_round_end(frame, prev_history: list[float], timeout_s: int = 120) -> list[float]:
    """
    Poll until a new crash value appears at the front of history.
    Returns the updated history list.
    """
    for _ in range(timeout_s * 4):
        hist = await get_crash_history(frame)
        if hist and (not prev_history or hist[0] != prev_history[0]):
            return hist
        await asyncio.sleep(0.25)
    raise TimeoutError("Round did not end within %ds" % timeout_s)


async def test_credentials(username: str, password: str, headless: bool = True) -> dict:
    """
    Try to log in to SportPesa with the given credentials.
    Returns {"ok": bool, "message": str}.
    Runs a temporary headless browser — does NOT start the game.
    """
    pw = None
    browser = None
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=headless, slow_mo=50)
        ctx  = await browser.new_context()
        page = await ctx.new_page()

        await page.goto(config.LOGIN_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        await page.fill(SEL["login_user"], username)
        await page.fill(SEL["login_pass"], password)
        await page.click(SEL["login_btn"])

        try:
            await page.wait_for_url(lambda u: "login" not in u, timeout=12_000)
            # Give the page a moment to render the balance widget
            await page.wait_for_timeout(2000)
            balance = await page.evaluate("""() => {
                const testIds = ['user-balance','balance','wallet-balance','account-balance','funds'];
                for (const id of testIds) {
                    const el = document.querySelector('[data-testid="' + id + '"]');
                    if (el && el.offsetParent !== null) { const t = el.innerText.trim(); if (t) return t; }
                }
                const keywords = ['balance', 'wallet', 'funds', 'amount', 'credit'];
                for (const kw of keywords) {
                    const els = document.querySelectorAll('[class*="' + kw + '"]');
                    for (const el of els) {
                        if (el.children.length === 0 && el.offsetParent !== null) {
                            const t = el.innerText.trim();
                            if (t && t.length < 40 && /[\\d]/.test(t)) return t;
                        }
                    }
                }
                const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                let node;
                while ((node = walk.nextNode())) {
                    const t = node.textContent.trim();
                    if (!t || t.length > 40) continue;
                    if (/KES/i.test(t) && /[\\d,]+/.test(t)) return t;
                }
                const navEls = document.querySelectorAll('header *, nav *, .header *, .navbar *');
                for (const el of navEls) {
                    if (el.children.length === 0 && el.offsetParent !== null) {
                        const t = el.innerText.trim();
                        if (/^[\\d,]+\\.\\d{2}$/.test(t)) return t;
                    }
                }
                return null;
            }""")
            return {"ok": True, "message": "Login successful — credentials are valid.", "balance": balance or "—"}
        except Exception:
            # Still on login page — check for an error message
            error_text = ""
            for sel in [".alert", ".error", ".notification", "[class*='error']", "[class*='alert']"]:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    error_text = (await el.inner_text()).strip()
                    break
            msg = error_text or "Login failed — invalid credentials."
            return {"ok": False, "message": msg}
    except Exception as exc:
        return {"ok": False, "message": f"Test error: {exc}"}
    finally:
        if browser:
            await browser.close()
        if pw:
            await pw.stop()


def calc_p1_bet(recovery_deficit: float) -> float:
    """
    P1 bet = round((deficit + RECOVERY_PROFIT_TARGET) / PANEL1_CASHOUT, 2).
    P2 always stays at 1 KES — only P1 scales.
    """
    if recovery_deficit <= 0:
        return 1.0
    # e.g. deficit=16, target=1, odds=6 → (17/6) = 2.83
    return max(1.0, round((recovery_deficit + config.RECOVERY_PROFIT_TARGET) / config.PANEL1_CASHOUT, 2))


def calc_round_pnl(crash_mult: float, p1_bet: float = 1.0) -> tuple[float, str]:
    """
    Return (net_pnl, description) for a round given the crash multiplier.
    Panel 1 uses p1_bet (martingale). Panel 2 always bets 1 KES.
    """
    p2_bet = config.BET_AMOUNT
    p1_win = crash_mult >= config.PANEL1_CASHOUT
    p2_win = crash_mult >= config.PANEL2_CASHOUT

    pnl = 0.0
    pnl += p1_bet * (config.PANEL1_CASHOUT - 1) if p1_win else -p1_bet
    pnl += p2_bet * (config.PANEL2_CASHOUT - 1) if p2_win else -p2_bet

    p1_tag = f"WIN@{config.PANEL1_CASHOUT:.0f}x" if p1_win else "LOSS"
    p2_tag = f"WIN@{config.PANEL2_CASHOUT:.0f}x" if p2_win else "LOSS"
    desc = f"P1={p1_tag}(bet={p1_bet})  P2={p2_tag}(bet=1)  crash={crash_mult:.2f}x"
    return pnl, desc


# ── CSV history writer ────────────────────────────────────────────────────────

class HistoryCSV:
    """
    Appends every round result to a CSV file for later pattern analysis.

    Columns:
      timestamp       — ISO-8601 local time the round ended
      crash_mult      — the multiplier at which the plane crashed (e.g. 3.45)
      trigger         — 1 if this crash was above TRIGGER_MULT, else 0
      mode            — "watch" or "bet"
      p1_result       — "win" | "loss" | "-"  (- when watching)
      p2_result       — "win" | "loss" | "-"
      round_pnl       — net KES for this round (0 when watching)
      session_pnl     — cumulative P&L within current betting burst
      cumulative_pnl  — total P&L across the whole session
    """

    COLUMNS = [
        "timestamp", "crash_mult", "trigger",
        "mode", "p1_result", "p2_result",
        "round_pnl", "session_pnl", "cumulative_pnl",
    ]

    def __init__(
        self,
        session_id: str = "local",
        panel1_cashout: float = None,
        panel2_cashout: float = None,
        trigger_mult: float = None,
    ):
        self._panel1_cashout = panel1_cashout if panel1_cashout is not None else config.PANEL1_CASHOUT
        self._panel2_cashout = panel2_cashout if panel2_cashout is not None else config.PANEL2_CASHOUT
        self._trigger_mult   = trigger_mult   if trigger_mult   is not None else config.TRIGGER_MULT
        os.makedirs("history", exist_ok=True)
        date_str = datetime.now().strftime("%Y%m%d")
        self.path = os.path.join("history", f"aviator_{date_str}_{session_id}.csv")
        # Write header only if the file is new
        write_header = not os.path.exists(self.path)
        self._fh  = open(self.path, "a", newline="", encoding="utf-8")
        self._csv = csv.DictWriter(self._fh, fieldnames=self.COLUMNS)
        if write_header:
            self._csv.writeheader()
        log.info("History CSV: %s", os.path.abspath(self.path))

    def record(
        self,
        crash_mult: float,
        mode: str,                  # "watch" | "bet"
        round_pnl: float = 0.0,
        session_pnl: float = 0.0,
        cumulative_pnl: float = 0.0,
    ):
        p1_win = crash_mult >= self._panel1_cashout
        p2_win = crash_mult >= self._panel2_cashout
        self._csv.writerow({
            "timestamp":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "crash_mult":     f"{crash_mult:.2f}",
            "trigger":        1 if crash_mult > self._trigger_mult else 0,
            "mode":           mode,
            "p1_result":      ("win" if p1_win else "loss") if mode == "bet" else "-",
            "p2_result":      ("win" if p2_win else "loss") if mode == "bet" else "-",
            "round_pnl":      f"{round_pnl:.2f}",
            "session_pnl":    f"{session_pnl:.2f}",
            "cumulative_pnl": f"{cumulative_pnl:.2f}",
        })
        self._fh.flush()   # write to disk immediately — no data lost on crash

    def close(self):
        self._fh.close()


# ── Bot ───────────────────────────────────────────────────────────────────────

class AviatorBot:

    def __init__(
        self,
        username: str = None,
        password: str = None,
        session_id: str = None,
        headless: bool = None,
        strategy: dict = None,
        demo_mode: bool = False,
        auto_logout: bool = True,
    ):
        self._username   = username   or config.USERNAME
        self._password   = password   or config.PASSWORD
        self._headless   = headless   if headless is not None else config.HEADLESS
        self._session_id = session_id or "local"
        self.DEMO_MODE   = demo_mode
        self.AUTO_LOGOUT = auto_logout

        self._stop_event = asyncio.Event()

        # Per-session logger so multiple sessions don't collide
        self.log = logging.getLogger(f"aviator-bot.{self._session_id}")

        # Strategy parameters — from passed dict, falling back to config defaults
        s = strategy or {}
        self._strategy_type     = s.get("strategy_type",    "fixed")  # "fixed" | "ai"
        self._strategy_raw      = s   # passed to ai_strategy.analyze() each round
        self._ai_overrides: dict = {}  # manually pushed via set_ai_params()
        self.AI_HISTORY_WINDOW  = int(s.get("ai_history_window", 10))  # rounds to analyze
        self.PANEL1_CASHOUT          = s.get("panel1_cashout",         config.PANEL1_CASHOUT)
        self.PANEL2_CASHOUT          = s.get("panel2_cashout",         config.PANEL2_CASHOUT)
        self.TRIGGER_MULT            = s.get("trigger_mult",           config.TRIGGER_MULT)
        self.LOW_STREAK_MAX          = s.get("low_streak_max",         config.LOW_STREAK_MAX)
        self.MAX_BET_ROUNDS          = s.get("max_bet_rounds",         config.MAX_BET_ROUNDS)
        self.RECOVERY_PROFIT_TARGET  = s.get("recovery_profit_target", config.RECOVERY_PROFIT_TARGET)
        self.STOP_ON_PROFIT          = s.get("stop_on_profit",         config.STOP_ON_PROFIT)
        self.STOP_ON_LOSS            = s.get("stop_on_loss",           config.STOP_ON_LOSS)
        self.BET_AMOUNT              = s.get("bet_amount",             config.BET_AMOUNT)
        self.LOW_STREAK_ROUNDS          = s.get("low_streak_rounds",          8)
        self.TRIGGER_MODE               = s.get("trigger_mode",               "both")
        self.RECOVERY_ENABLED           = s.get("recovery_enabled",           True)
        self.RECOVERY_SCOPE             = s.get("recovery_scope",             "individual")
        self.RECOVERY_PERCENTAGE        = s.get("recovery_percentage",        100)
        self.BURST_COOLDOWN             = s.get("burst_cooldown",             0)
        self.STOP_ON_CONSECUTIVE_LOSSES = s.get("stop_on_consecutive_losses", 0)
        # Panel 2 independent recovery
        self.P2_BET_AMOUNT             = s.get("p2_bet_amount",             self.BET_AMOUNT)
        self.P2_RECOVERY_ENABLED       = s.get("p2_recovery_enabled",       False)
        self.P2_RECOVERY_PROFIT_TARGET = s.get("p2_recovery_profit_target", self.RECOVERY_PROFIT_TARGET)
        self.P2_RECOVERY_SCOPE         = s.get("p2_recovery_scope",         "individual")
        self.P2_RECOVERY_PERCENTAGE    = s.get("p2_recovery_percentage",    100)
        self.RECOVERY_STEPS            = s.get("recovery_steps",            0)
        self.P2_RECOVERY_STEPS         = s.get("p2_recovery_steps",         0)

        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page:    Optional[Page]    = None

        self.total_rounds = 0
        self.total_wins   = 0
        self.total_losses = 0
        self.cumulative_pnl = 0.0

        self.recovery_deficit    = 0.0
        self.p2_recovery_deficit = 0.0
        self.p1_bet = self.BET_AMOUNT
        self.p2_bet = self.P2_BET_AMOUNT
        self.last_event = "idle"
        self.account_balance = "—"

        self._cooldown_rounds    = 0   # watch rounds to skip after a burst
        self._consecutive_losses = 0   # running count of consecutive round losses
        self._rounds_left        = 0   # rounds remaining in current burst
        self._p1_step            = 0   # persistent pct-recovery step for P1 (carries across bursts)
        self._p2_step            = 0   # persistent pct-recovery step for P2

        self.csv = HistoryCSV(
            session_id=self._session_id,
            panel1_cashout=self.PANEL1_CASHOUT,
            panel2_cashout=self.PANEL2_CASHOUT,
            trigger_mult=self.TRIGGER_MULT,
        )

    def _p1_bet(self) -> float:
        if not self.RECOVERY_ENABLED:
            return self.BET_AMOUNT
        p1d = self.recovery_deficit
        p2d = self.p2_recovery_deficit
        if self.RECOVERY_SCOPE == "individual":
            target = p1d
        elif self.RECOVERY_SCOPE == "combined":
            target = p1d + p2d
        else:  # "percentage"
            total = p1d + p2d
            max_steps = self.RECOVERY_STEPS if self.RECOVERY_STEPS > 0 else self.MAX_BET_ROUNDS
            is_last = (self._p1_step + 1) >= max_steps
            target = total if is_last else total * self.RECOVERY_PERCENTAGE / 100
        if target <= 0:
            return self.BET_AMOUNT
        return max(self.BET_AMOUNT,
                   round((target + self.RECOVERY_PROFIT_TARGET) / self.PANEL1_CASHOUT, 2))

    def _p2_bet(self) -> float:
        if not self.P2_RECOVERY_ENABLED:
            return self.P2_BET_AMOUNT
        p1d = self.recovery_deficit
        p2d = self.p2_recovery_deficit
        if self.P2_RECOVERY_SCOPE == "individual":
            target = p2d
        elif self.P2_RECOVERY_SCOPE == "combined":
            target = p1d + p2d
        else:  # "percentage"
            total = p1d + p2d
            max_steps = self.P2_RECOVERY_STEPS if self.P2_RECOVERY_STEPS > 0 else self.MAX_BET_ROUNDS
            is_last = (self._p2_step + 1) >= max_steps
            target = total if is_last else total * self.P2_RECOVERY_PERCENTAGE / 100
        if target <= 0:
            return self.P2_BET_AMOUNT
        return max(self.P2_BET_AMOUNT,
                   round((target + self.P2_RECOVERY_PROFIT_TARGET) / self.PANEL2_CASHOUT, 2))

    def _round_pnl(self, crash_mult: float, p1_bet: float, p2_bet: float) -> tuple[float, str]:
        p1_win = crash_mult >= self.PANEL1_CASHOUT
        p2_win = crash_mult >= self.PANEL2_CASHOUT
        pnl = 0.0
        pnl += p1_bet * (self.PANEL1_CASHOUT - 1) if p1_win else -p1_bet
        pnl += p2_bet * (self.PANEL2_CASHOUT - 1) if p2_win else -p2_bet
        p1_tag = f"WIN@{self.PANEL1_CASHOUT:.0f}x" if p1_win else "LOSS"
        p2_tag = f"WIN@{self.PANEL2_CASHOUT:.0f}x" if p2_win else "LOSS"
        desc = f"P1={p1_tag}(bet={p1_bet})  P2={p2_tag}(bet={p2_bet})  crash={crash_mult:.2f}x"
        return pnl, desc

    def request_stop(self):
        self._stop_event.set()

    def set_ai_params(self, params: dict):
        """Push manual parameter overrides into the AI loop. Applied on the next round.
        Accepted keys: bet_amount, p2_bet_amount, panel1_cashout, panel2_cashout."""
        allowed  = {"bet_amount", "p2_bet_amount", "panel1_cashout", "panel2_cashout"}
        filtered = {k: v for k, v in params.items() if k in allowed and v is not None}
        self._ai_overrides.update(filtered)
        self.log.info("AI manual overrides updated: %s", self._ai_overrides)

    async def run_ai(self):
        """
        AI strategy loop — completely independent of the fixed trigger/recovery logic.

        Once AI_HISTORY_WINDOW rounds of history are available, every round:
          1. ai_strategy.analyze(history[:window]) → suggested params
          2. merge with manual overrides from set_ai_params()
          3. apply bet_amount / p2_bet_amount / panel1_cashout / panel2_cashout to game UI
          4. place bets on both panels
          5. record result
        """
        frame = await self._wait_for_frame(timeout_s=30)
        await self.setup_panels(frame)

        history     = await get_crash_history(frame)
        session_pnl = 0.0

        self.last_event = f"AI: collecting history (0/{self.AI_HISTORY_WINDOW})"
        self.log.info("=" * 60)
        self.log.info("AI strategy active — history window: %d rounds", self.AI_HISTORY_WINDOW)
        self.log.info("  Baseline  P1: %.2f KES @ %.1fx | P2: %.2f KES @ %.1fx",
                      self.BET_AMOUNT, self.PANEL1_CASHOUT, self.P2_BET_AMOUNT, self.PANEL2_CASHOUT)
        self.log.info("=" * 60)

        while True:
            if self._stop_event.is_set():
                self.last_event = "stopped"
                break
            reason = self.should_stop()
            if reason:
                self.last_event = reason
                self.log.info("AI: stopping — %s", reason)
                break

            frame = self._get_frame()
            if frame is None:
                self.log.warning("AI: game frame lost — waiting…")
                try:
                    frame = await self._wait_for_frame(timeout_s=30)
                except TimeoutError:
                    self.log.error("AI: frame never came back — aborting.")
                    break

            self.last_event = "AI: waiting for next round…"
            try:
                ok = await wait_for_bet_phase(frame)
            except Exception as e:
                self.log.warning("AI: frame lost during bet-phase wait (%s) — retrying.", e)
                continue
            if not ok:
                self.log.error("AI: bet phase never opened — aborting.")
                break

            # ── Not enough history yet — watch without betting ────────────────
            if len(history) < self.AI_HISTORY_WINDOW:
                self.last_event = (
                    f"AI: collecting history ({len(history)}/{self.AI_HISTORY_WINDOW})"
                )
                self.log.info("AI: history %d/%d — watching.", len(history), self.AI_HISTORY_WINDOW)
                try:
                    prev    = await get_crash_history(frame)
                    history = await wait_for_round_end(frame, prev)
                except Exception as e:
                    self.log.warning("AI: frame stale collecting history (%s).", e)
                continue

            # ── Analyze and resolve params ────────────────────────────────────
            computed  = ai_strategy.analyze(history[:self.AI_HISTORY_WINDOW])
            overrides = {**computed, **self._ai_overrides}   # manual wins over computed

            p1_bet     = float(overrides.get("bet_amount",     self.BET_AMOUNT))
            p2_bet     = float(overrides.get("p2_bet_amount",  self.P2_BET_AMOUNT))
            p1_cashout = float(overrides.get("panel1_cashout", self.PANEL1_CASHOUT))
            p2_cashout = float(overrides.get("panel2_cashout", self.PANEL2_CASHOUT))

            # Reconfigure game UI if cashout targets changed
            try:
                if p1_cashout != self.PANEL1_CASHOUT:
                    await self._setup_one_panel(frame, 0, p1_cashout, p1_bet)
                    self.PANEL1_CASHOUT = p1_cashout
                elif p1_bet != self.p1_bet:
                    await self._set_panel1_bet(frame, p1_bet)

                if p2_cashout != self.PANEL2_CASHOUT:
                    await self._setup_one_panel(frame, 1, p2_cashout, p2_bet)
                    self.PANEL2_CASHOUT = p2_cashout
                elif p2_bet != self.p2_bet:
                    await self._set_panel2_bet(frame, p2_bet)
            except Exception as e:
                self.log.warning("AI: failed to update panel config (%s) — using previous.", e)

            self.p1_bet = p1_bet
            self.p2_bet = p2_bet
            self.last_event = (
                f"AI betting — P1={p1_bet:.2f}@{p1_cashout:.1f}x  "
                f"P2={p2_bet:.2f}@{p2_cashout:.1f}x"
            )
            self.log.info(
                "AI: P1=%.2f@%.1fx  P2=%.2f@%.1fx  (window=%d, overrides=%s)",
                p1_bet, p1_cashout, p2_bet, p2_cashout,
                self.AI_HISTORY_WINDOW, overrides or "none",
            )

            # ── Place bets ────────────────────────────────────────────────────
            try:
                prev   = await get_crash_history(frame)
                placed = await self.place_bets(frame)
            except Exception as e:
                self.log.warning("AI: frame stale placing bets (%s) — skipping round.", e)
                continue

            if not placed:
                self.log.warning("AI: could not place bets — skipping round.")
                try:
                    prev2   = await get_crash_history(frame)
                    history = await wait_for_round_end(frame, prev2)
                except Exception:
                    pass
                continue

            # ── Wait for round end ────────────────────────────────────────────
            try:
                history = await wait_for_round_end(frame, prev)
            except TimeoutError:
                self.log.error("AI: round end timeout — continuing.")
                continue
            except Exception as e:
                self.log.warning("AI: frame stale waiting for round end (%s).", e)
                continue

            crash_mult          = history[0]
            round_pnl, desc     = self._round_pnl(crash_mult, p1_bet, p2_bet)
            session_pnl         += round_pnl
            self.cumulative_pnl += round_pnl
            self.total_rounds   += 1

            if round_pnl > 0:
                self.total_wins   += 1
            else:
                self.total_losses += 1

            self.csv.record(
                crash_mult, mode="bet",
                round_pnl=round_pnl,
                session_pnl=session_pnl,
                cumulative_pnl=self.cumulative_pnl,
            )
            self.last_event = (
                f"AI Round {self.total_rounds}: crash={crash_mult:.2f}x "
                f"round={round_pnl:+.2f} total={self.cumulative_pnl:.2f} KES"
            )
            self.log.info(
                "AI ROUND %d | %s | round=%+.2f KES | total=%.2f KES",
                self.total_rounds, desc, round_pnl, self.cumulative_pnl,
            )
            await self._read_balance()

    # ── Browser ───────────────────────────────────────────────────────────────

    async def start(self):
        pw = await async_playwright().start()
        self.browser = await pw.chromium.launch(
            headless=self._headless,
            slow_mo=config.SLOW_MO,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
        )
        self.context = await self.browser.new_context(
            no_viewport=True,
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        self.context.set_default_timeout(config.BROWSER_TIMEOUT)
        self.page = await self.context.new_page()

    async def logout(self):
        """Log out of SportPesa before closing the browser."""
        if not self.page:
            return
        try:
            self.last_event = "Logging out…"
            self.log.info("Logging out of SportPesa…")
            # Try direct logout URL first
            await self.page.goto(
                f"{config.BASE_URL}/logout",
                wait_until="domcontentloaded",
                timeout=8_000,
            )
            await self.page.wait_for_timeout(1500)
            # Verify we landed on login/home (not still on a user page)
            if "logout" not in self.page.url and "login" not in self.page.url:
                # Fallback: click a logout button in the UI
                for sel in [
                    '[data-testid="logout"]',
                    'a[href*="logout"]',
                    'button:has-text("Logout")',
                    'button:has-text("Sign out")',
                    'a:has-text("Logout")',
                ]:
                    try:
                        el = await self.page.query_selector(sel)
                        if el and await el.is_visible():
                            await el.click()
                            await self.page.wait_for_timeout(1500)
                            break
                    except Exception:
                        continue
            self.last_event = "Logged out"
            self.log.info("Logout complete.")
        except Exception as e:
            self.log.warning("Logout attempt failed: %s", e)

    async def stop(self):
        if self.browser:
            await self.browser.close()

    # ── Login ─────────────────────────────────────────────────────────────────

    async def login(self):
        self.last_event = "Logging in…"
        self.log.info("Logging in…")
        await self.page.goto(config.LOGIN_URL, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(2000)
        await self.page.fill(SEL["login_user"], self._username)
        await self.page.fill(SEL["login_pass"], self._password)
        await self.page.click(SEL["login_btn"])
        try:
            await self.page.wait_for_url(lambda u: "login" not in u, timeout=15_000)
            self.last_event = "Login successful"
            self.log.info("Login successful.")
            await self._dismiss_page_popups()
            await self._read_balance()
        except PWTimeout:
            self.last_event = "Login failed — check credentials"
            self.log.error("Login may have failed — still on login page.")
            raise

    # ── Account balance ───────────────────────────────────────────────────────

    async def _read_balance(self):
        """Read account balance.
        Demo mode: read from the Spribe game iframe (demo wallet shown there).
        Real mode: read from the SportPesa header on the main page.
        """
        if not self.page:
            return
        try:
            await asyncio.sleep(1.5)

            if self.DEMO_MODE:
                # Demo balance lives inside the game frame, not the host page
                frame = self._get_frame()
                if frame:
                    balance = await frame.evaluate("""() => {
                        // Spribe shows demo wallet in elements with class containing balance
                        const selectors = [
                            '[class*="balance"]', '[class*="wallet"]',
                            '[class*="credit"]',  '[class*="currency"]',
                            '[class*="amount"]',  '[data-testid*="balance"]',
                        ];
                        for (const sel of selectors) {
                            const els = document.querySelectorAll(sel);
                            for (const el of els) {
                                if (el.children.length === 0 && el.offsetParent !== null) {
                                    const t = el.innerText.trim();
                                    if (t && /[\\d]/.test(t) && t.length < 30) return 'Demo: ' + t;
                                }
                            }
                        }
                        // Fallback: any leaf with a decimal number
                        const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                        let node;
                        while ((node = walk.nextNode())) {
                            const t = node.textContent.trim();
                            if (/^[\\d,\\.]+$/.test(t) && t.length < 15) return 'Demo: ' + t;
                        }
                        return null;
                    }""")
                    if balance:
                        self.account_balance = balance
                        self.log.info("Demo balance: %s", balance)
                    else:
                        self.log.debug("Demo balance not found in frame yet.")
                return

            # Real-money balance from SportPesa header
            balance = await self.page.evaluate("""() => {
                // 1. data-testid attributes
                const testIds = ['user-balance','balance','wallet-balance','account-balance','funds'];
                for (const id of testIds) {
                    const el = document.querySelector('[data-testid="' + id + '"]');
                    if (el && el.offsetParent !== null) {
                        const t = el.innerText.trim();
                        if (t) return t;
                    }
                }

                // 2. Class name contains balance/wallet/funds/amount
                const keywords = ['balance', 'wallet', 'funds', 'amount', 'credit'];
                for (const kw of keywords) {
                    const els = document.querySelectorAll('[class*="' + kw + '"]');
                    for (const el of els) {
                        if (el.children.length === 0 && el.offsetParent !== null) {
                            const t = el.innerText.trim();
                            if (t && t.length < 40 && /[\\d]/.test(t)) return t;
                        }
                    }
                }

                // 3. Walk every leaf text node — look for KES or a plain decimal
                const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                let node;
                while ((node = walk.nextNode())) {
                    const t = node.textContent.trim();
                    if (!t || t.length > 40) continue;
                    if (/KES/i.test(t) && /[\\d,]+/.test(t)) return t;
                }

                // 4. Header/nav numbers that look like a balance (e.g. "1,234.56")
                const navEls = document.querySelectorAll('header *, nav *, .header *, .navbar *');
                for (const el of navEls) {
                    if (el.children.length === 0 && el.offsetParent !== null) {
                        const t = el.innerText.trim();
                        if (/^[\\d,]+\\.\\d{2}$/.test(t)) return t;
                    }
                }

                return null;
            }""")

            if balance:
                self.account_balance = balance
                self.log.info("Balance: %s", balance)
            else:
                self.log.warning("Balance not found — page may still be loading or selector changed")
        except Exception as e:
            self.log.debug("Balance read failed: %s", e)

    # ── Popup & mode handling ─────────────────────────────────────────────────

    async def _dismiss_page_popups(self):
        """Close SportPesa modals (Quick Deposit, cookie prompts, etc.) on the main page."""
        await asyncio.sleep(1.2)   # give popup time to appear after navigation
        for sel in [
            # Bootstrap-style close buttons inside modals
            '.modal.show .close',
            '.modal.show button[data-dismiss="modal"]',
            '.modal.show [aria-label="Close"]',
            # Generic close/dismiss patterns
            'button[data-dismiss="modal"]',
            '.modal__close',
            '.dialog__close',
            '.popup__close',
            '[data-testid="modal-close-button"]',
            '[aria-label="Close"]',
            '[aria-label="close"]',
            # Quick Deposit specific
            '.quick-deposit .close',
            '.deposit-modal .close',
            # Fallback: any visible ✕ / × button
            'button.close:visible',
        ]:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    await asyncio.sleep(0.5)
                    self.log.info("Dismissed popup: %s", sel)
                    return
            except Exception:
                continue
        # Last resort — Escape key
        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass

    async def _select_demo_mode(self, frame):
        """
        Click the Demo / Try for Free button if the Spribe mode-selection screen appears.
        Called only when DEMO_MODE is True.
        """
        await asyncio.sleep(0.8)
        for sel in [
            'button:has-text("Demo")',
            'button:has-text("Try for free")',
            'button:has-text("Try For Free")',
            'button:has-text("Fun")',
            'button:has-text("Practice")',
            '[data-testid="demo-button"]',
            '[class*="demo-btn"]',
            '[class*="fun-btn"]',
        ]:
            try:
                el = await frame.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    await asyncio.sleep(1.0)
                    self.log.info("Demo mode selected via: %s", sel)
                    return
            except Exception:
                continue
        self.log.info("No Demo mode selector found in frame — game already in a playable state.")

    # ── Open game ─────────────────────────────────────────────────────────────

    def _get_frame(self):
        """
        Always return the CURRENT live Spribe frame from page.frames.
        Never cache the frame object — the iframe reloads periodically
        (new token), which destroys the old execution context.
        """
        for f in self.page.frames:
            if "spribegaming.com" in f.url or "aviator-next" in f.url:
                return f
        return None

    async def _wait_for_frame(self, timeout_s=30):
        """Poll until the Spribe frame is present and has bet inputs loaded."""
        demo_attempted = False
        for _ in range(timeout_s * 2):
            frame = self._get_frame()
            if frame:
                try:
                    # If Demo mode is on and we haven't tried yet, do it now
                    if self.DEMO_MODE and not demo_attempted:
                        await self._select_demo_mode(frame)
                        demo_attempted = True
                    inputs = await frame.query_selector_all('input')
                    if inputs:
                        return frame
                except Exception:
                    pass
            await asyncio.sleep(0.5)
        raise TimeoutError("Spribe game frame with inputs not ready after %ds" % timeout_s)

    async def open_aviator(self):
        self.last_event = "Opening Aviator page…"
        self.log.info("Opening Aviator…")
        await self.page.goto(config.AVIATOR_URL, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(1500)
        try:
            await self.page.click(SEL["cookie_accept"], timeout=4_000)
            self.log.info("Cookie banner dismissed.")
        except PWTimeout:
            pass
        # Close any deposit/balance popups before the game loads
        await self._dismiss_page_popups()
        self.last_event = "Waiting for game to load…"
        self.log.info("Waiting for Spribe game frame + inputs…")
        frame = await self._wait_for_frame(timeout_s=30)
        self.last_event = "Game loaded — setting up panels"
        self.log.info("Game ready: %s", frame.url[:70])
        await self.page.wait_for_timeout(1000)
        return frame

    # ── One-time panel setup ──────────────────────────────────────────────────

    async def _setup_one_panel(self, frame, panel_idx: int, cashout_target: float, bet_amount: float = None):
        """
        Configure a single betting panel (0 = top, 1 = bottom):
          1. Click the "Auto" tab on that panel  → reveals Auto Cash Out toggle
          2. Enable the Auto Cash Out toggle      → reveals the cashout odds input
          3. Set the cashout odds to target
          4. Set the bet amount to 1 KES
        """
        # All "Bet/Auto" tab pairs — each panel has exactly one "Auto" tab
        auto_tabs = await frame.query_selector_all('button.tab')
        auto_tabs = [t for t in auto_tabs if (await t.inner_text()).strip() == "Auto"]
        if panel_idx >= len(auto_tabs):
            self.log.warning("Panel %d Auto tab not found (only %d tabs)", panel_idx, len(auto_tabs))
            return
        auto_tab = auto_tabs[panel_idx]

        # Click Auto tab if not already active
        cls = await auto_tab.get_attribute("class") or ""
        if "active" not in cls:
            await auto_tab.click()
            await asyncio.sleep(0.5)
            self.log.info("  Panel %d: clicked Auto tab.", panel_idx)

        # Now enable the Auto Cash Out toggle (it says "off" in its class when disabled)
        # Each panel has its own cash-out-switcher; grab by index
        switchers = await frame.query_selector_all('.cash-out-switcher')
        if panel_idx < len(switchers):
            toggle = await switchers[panel_idx].query_selector('.input-switch')
            if toggle:
                cls = await toggle.get_attribute("class") or ""
                if "off" in cls:
                    await toggle.click()
                    await asyncio.sleep(0.5)
                    self.log.info("  Panel %d: Auto Cash Out toggle enabled.", panel_idx)
                else:
                    self.log.info("  Panel %d: Auto Cash Out toggle already ON.", panel_idx)
        else:
            self.log.warning("  Panel %d: cash-out-switcher not found.", panel_idx)

        # Find cashout inputs directly — one per wrapper, no dedup needed
        spinner_inputs = []
        for inp in await frame.query_selector_all('.cashout-spinner-wrapper input'):
            if await inp.is_visible():
                spinner_inputs.append(inp)

        if panel_idx < len(spinner_inputs):
            inp = spinner_inputs[panel_idx]
            cur = await inp.input_value()
            self.log.info("  Panel %d: cashout input found (current=%r). Setting to %s…", panel_idx, cur, cashout_target)
            await set_input(inp, cashout_target)
            after = await inp.input_value()
            self.log.info("  Panel %d: cashout value is now %r", panel_idx, after)
        else:
            self.log.warning("  Panel %d: cashout spinner input not found (%d found).", panel_idx, len(spinner_inputs))

        # Set the bet amount
        _bet = bet_amount if bet_amount is not None else self.BET_AMOUNT
        bet_inputs = await frame.query_selector_all('input[placeholder="1"]')
        if panel_idx < len(bet_inputs):
            await set_input(bet_inputs[panel_idx], _bet)
            self.log.info("  Panel %d: bet amount set to %s KES.", panel_idx, _bet)

    async def setup_panels(self, frame):
        """
        Set up both panels:
          - Auto tab → enables Auto Cash Out toggle
          - Auto Cash Out toggle ON → reveals cashout odds input
          - Panel 1 cashout: PANEL1_CASHOUT (6x)
          - Panel 2 cashout: PANEL2_CASHOUT (3x)
          - Both bets: BET_AMOUNT (1 KES)
        """
        self.last_event = "Setting up Panel 1…"
        self.log.info("Setting up Panel 1 (cashout=%.1fx, bet=%s KES)…",
                 self.PANEL1_CASHOUT, self.BET_AMOUNT)
        await self._setup_one_panel(frame, panel_idx=0,
                                    cashout_target=self.PANEL1_CASHOUT,
                                    bet_amount=self.BET_AMOUNT)

        self.last_event = "Setting up Panel 2…"
        self.log.info("Setting up Panel 2 (cashout=%.1fx, bet=%s KES)…",
                 self.PANEL2_CASHOUT, self.P2_BET_AMOUNT)
        await self._setup_one_panel(frame, panel_idx=1,
                                    cashout_target=self.PANEL2_CASHOUT,
                                    bet_amount=self.P2_BET_AMOUNT)

        # ── Verify all visible inputs ─────────────────────────────────────────
        await asyncio.sleep(0.4)
        visible_vals = []
        for inp in await frame.query_selector_all('input'):
            if await inp.is_visible():
                visible_vals.append(await inp.input_value())
        self.log.info("Visible input values after setup: %s", visible_vals)
        self.log.info("Setup complete — P1 bet=1 @%.1fx | P2 bet=1 @%.1fx",
                 self.PANEL1_CASHOUT, self.PANEL2_CASHOUT)

    # ── Panel 1 martingale bet update ─────────────────────────────────────────

    async def _set_panel1_bet(self, frame, amount: float):
        bet_inputs = await frame.query_selector_all('input[placeholder="1"]')
        if bet_inputs:
            await set_input(bet_inputs[0], amount)
            self.log.info("P1 bet → %.2f KES (P1 deficit: %.2f KES).", amount, self.recovery_deficit)

    async def _set_panel2_bet(self, frame, amount: float):
        bet_inputs = await frame.query_selector_all('input[placeholder="1"]')
        if len(bet_inputs) > 1:
            await set_input(bet_inputs[1], amount)
            self.log.info("P2 bet → %.2f KES (P2 deficit: %.2f KES).", amount, self.p2_recovery_deficit)

    # ── Place bets on both panels ─────────────────────────────────────────────

    async def place_bets(self, frame) -> bool:
        btns = await frame.query_selector_all(SEL["bet_btn"])
        if not btns:
            self.log.warning("BET buttons not found — bet phase may have already closed.")
            return False
        await btns[0].click()
        if len(btns) > 1:
            await asyncio.sleep(0.1)
            await btns[1].click()
        self.log.info("Bets placed on %d panel(s).", min(len(btns), 2))
        return True

    # ── Global stop checks ────────────────────────────────────────────────────

    def should_stop(self) -> Optional[str]:
        if self.cumulative_pnl >= self.STOP_ON_PROFIT:
            return f"Profit target reached (KES {self.cumulative_pnl:.2f})"
        if self.cumulative_pnl <= self.STOP_ON_LOSS:
            return f"Loss limit hit (KES {self.cumulative_pnl:.2f})"
        return None

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self):
        await self.start()
        try:
            await self.login()
            frame = await self.open_aviator()

            if self._strategy_type == "ai":
                await self.run_ai()
                return   # finally block still runs (summary / logout / stop)

            await self.setup_panels(frame)

            # State
            watching     = True    # True = watching for trigger
            bet_next     = False   # True = place bets when bet phase opens
            rounds_left  = 0       # rounds remaining in current betting burst
            session_pnl  = 0.0    # P&L since last trigger
            history      = await get_crash_history(frame)

            self.last_event = "Strategy active — watching for trigger"
            self.log.info("=" * 60)
            self.log.info("Strategy active")
            self.log.info("  Trigger mode  : %s", self.TRIGGER_MODE)
            self.log.info("  Trigger mult  : last crash > %.1fx", self.TRIGGER_MULT)
            self.log.info("  Low streak    : %d crashes all ≤ %.1fx", self.LOW_STREAK_ROUNDS, self.LOW_STREAK_MAX)
            self.log.info("  Max rounds per burst : %d", self.MAX_BET_ROUNDS)
            self.log.info("  Panel 1 : KES %.2f  auto-cashout @ %.1fx  recovery=%s",
                          self.BET_AMOUNT, self.PANEL1_CASHOUT,
                          "ON" if self.RECOVERY_ENABLED else "OFF")
            self.log.info("  Panel 2 : KES %.2f  auto-cashout @ %.1fx  recovery=%s",
                          self.P2_BET_AMOUNT, self.PANEL2_CASHOUT,
                          "ON" if self.P2_RECOVERY_ENABLED else "OFF")
            self.log.info("  Stop profit : KES %.0f  |  Stop loss : KES %.0f", self.STOP_ON_PROFIT, self.STOP_ON_LOSS)
            self.log.info("  Burst cooldown : %d rounds  |  Max consec. losses : %d (0=off)",
                          self.BURST_COOLDOWN, self.STOP_ON_CONSECUTIVE_LOSSES)
            self.log.info("=" * 60)

            while True:
                # Stop if requested remotely or by profit/loss guard
                if self._stop_event.is_set():
                    self.log.info("Stop requested — exiting.")
                    self.last_event = "stopped"
                    break

                reason = self.should_stop()
                if reason:
                    self.log.info("Bot stopping: %s", reason)
                    self.last_event = reason
                    break

                # Always use a fresh frame reference — the iframe reloads periodically
                frame = self._get_frame()
                if frame is None:
                    self.log.warning("Game frame lost — waiting for it to reload…")
                    try:
                        frame = await self._wait_for_frame(timeout_s=30)
                        self.log.info("Frame recovered.")
                    except TimeoutError:
                        self.log.error("Frame never came back — aborting.")
                        break

                # Wait for the betting window to open
                self.last_event = "Waiting for next round…"
                self.log.info("Waiting for bet phase…")
                try:
                    ok = await wait_for_bet_phase(frame)
                except Exception as e:
                    self.log.warning("Frame context lost during bet-phase wait (%s) — retrying.", e)
                    continue
                if not ok:
                    self.log.error("Bet phase never opened — aborting.")
                    break

                if bet_next:
                    # ── Betting round ─────────────────────────────────────────
                    try:
                        self.p1_bet = self._p1_bet()
                        self.p2_bet = self._p2_bet()
                        self.last_event = (
                            f"Placing bets — P1={self.p1_bet:.2f} KES, P2={self.p2_bet:.2f} KES"
                        )
                        if self.p1_bet != self.BET_AMOUNT:
                            await self._set_panel1_bet(frame, self.p1_bet)
                        if self.p2_bet != self.P2_BET_AMOUNT:
                            await self._set_panel2_bet(frame, self.p2_bet)
                        prev_history = await get_crash_history(frame)
                        placed = await self.place_bets(frame)
                    except Exception as e:
                        self.log.warning("Frame stale placing bet (%s) — skipping round.", e)
                        rounds_left -= 1
                        self._rounds_left = rounds_left
                        continue

                    if not placed:
                        self.log.warning("Could not place bets — skipping round.")
                        rounds_left -= 1
                        self._rounds_left = rounds_left
                    else:
                        # Wait for round to finish
                        try:
                            history = await wait_for_round_end(frame, prev_history)
                        except TimeoutError:
                            self.log.error("Round end timeout — resetting to watch mode.")
                            watching, bet_next = True, False
                            continue
                        except Exception as e:
                            self.log.warning("Frame stale waiting for round end (%s) — resetting.", e)
                            watching, bet_next = True, False
                            continue

                        crash_mult = history[0]
                        round_pnl, desc = self._round_pnl(crash_mult, self.p1_bet, self.p2_bet)
                        session_pnl         += round_pnl
                        self.cumulative_pnl += round_pnl
                        self.total_rounds   += 1
                        rounds_left         -= 1
                        self._rounds_left    = rounds_left   # keep in sync for bet-sizing

                        # ── P1 deficit (resets when crash >= P1 cashout) ──────
                        if crash_mult >= self.PANEL1_CASHOUT:
                            if self.RECOVERY_SCOPE == "percentage":
                                total = self.recovery_deficit + self.p2_recovery_deficit
                                max_steps = self.RECOVERY_STEPS if self.RECOVERY_STEPS > 0 else self.MAX_BET_ROUNDS
                                was_last  = (self._p1_step + 1) >= max_steps
                                target = total if was_last else total * self.RECOVERY_PERCENTAGE / 100
                                new_combined = round(max(0.0, total - target), 2)
                                self.log.info(
                                    "P1 won at %.2fx — %s → %.2f KES deficit remaining.",
                                    crash_mult,
                                    "full recovery (last round)" if was_last
                                    else f"{self.RECOVERY_PERCENTAGE}% recovery",
                                    new_combined,
                                )
                                self.recovery_deficit = new_combined
                                self.p2_recovery_deficit = 0.0
                            else:
                                self.log.info(
                                    "P1 won at %.2fx — P1 deficit cleared (was %.2f KES).",
                                    crash_mult, self.recovery_deficit,
                                )
                                self.recovery_deficit = 0.0
                                if self.RECOVERY_SCOPE == "combined":
                                    self.p2_recovery_deficit = 0.0
                                    self.log.info("Combined scope — P2 deficit also cleared.")
                            self._consecutive_losses = 0
                        else:
                            if self.RECOVERY_ENABLED:
                                self.recovery_deficit = round(
                                    self.recovery_deficit + self.p1_bet, 2
                                )
                                self.log.info(
                                    "P1 deficit = %.2f KES → next P1 bet = %.2f KES.",
                                    self.recovery_deficit, self._p1_bet(),
                                )
                            self._consecutive_losses += 1
                            if (self.STOP_ON_CONSECUTIVE_LOSSES > 0
                                    and self._consecutive_losses >= self.STOP_ON_CONSECUTIVE_LOSSES):
                                self.log.info(
                                    "Consecutive loss limit reached (%d) — stopping session.",
                                    self._consecutive_losses,
                                )
                                self.last_event = (
                                    f"Stopped: {self._consecutive_losses} consecutive losses"
                                )
                                break

                        # ── P2 deficit ───────────────────────────────────────
                        if crash_mult >= self.PANEL2_CASHOUT:
                            if self.P2_RECOVERY_SCOPE == "percentage":
                                total = self.recovery_deficit + self.p2_recovery_deficit
                                max_steps = self.P2_RECOVERY_STEPS if self.P2_RECOVERY_STEPS > 0 else self.MAX_BET_ROUNDS
                                was_last  = (self._p2_step + 1) >= max_steps
                                target = total if was_last else total * self.P2_RECOVERY_PERCENTAGE / 100
                                new_combined = round(max(0.0, total - target), 2)
                                self.log.info(
                                    "P2 won at %.2fx — %s → %.2f KES deficit remaining.",
                                    crash_mult,
                                    "full recovery (last round)" if was_last
                                    else f"{self.P2_RECOVERY_PERCENTAGE}% recovery",
                                    new_combined,
                                )
                                self.p2_recovery_deficit = new_combined
                                self.recovery_deficit = 0.0
                            elif self.P2_RECOVERY_SCOPE == "combined":
                                self.log.info(
                                    "P2 won at %.2fx (combined scope) — clearing both deficits.",
                                    crash_mult,
                                )
                                self.p2_recovery_deficit = 0.0
                                self.recovery_deficit = 0.0
                            else:  # "individual"
                                self.p2_recovery_deficit = 0.0
                        elif self.P2_RECOVERY_ENABLED:
                            self.p2_recovery_deficit = round(
                                self.p2_recovery_deficit + self.p2_bet, 2
                            )
                            self.log.info(
                                "P2 deficit = %.2f KES → next P2 bet = %.2f KES.",
                                self.p2_recovery_deficit, self._p2_bet(),
                            )

                        # Advance persistent percentage step counters (carry across bursts)
                        if self.RECOVERY_SCOPE == "percentage" and self.RECOVERY_ENABLED:
                            total_def = self.recovery_deficit + self.p2_recovery_deficit
                            if total_def <= 0:
                                self._p1_step = 0  # deficit cleared — fresh cycle
                            else:
                                max_s = self.RECOVERY_STEPS if self.RECOVERY_STEPS > 0 else self.MAX_BET_ROUNDS
                                self._p1_step = 0 if (self._p1_step + 1) >= max_s else self._p1_step + 1
                        if self.P2_RECOVERY_SCOPE == "percentage" and self.P2_RECOVERY_ENABLED:
                            total_def = self.recovery_deficit + self.p2_recovery_deficit
                            if total_def <= 0:
                                self._p2_step = 0
                            else:
                                max_s = self.P2_RECOVERY_STEPS if self.P2_RECOVERY_STEPS > 0 else self.MAX_BET_ROUNDS
                                self._p2_step = 0 if (self._p2_step + 1) >= max_s else self._p2_step + 1

                        if round_pnl > 0:
                            self.total_wins += 1
                        else:
                            self.total_losses += 1

                        self.csv.record(
                            crash_mult, mode="bet",
                            round_pnl=round_pnl,
                            session_pnl=session_pnl,
                            cumulative_pnl=self.cumulative_pnl,
                        )
                        self.last_event = (
                            f"Round {self.total_rounds}: crash={crash_mult:.2f}x "
                            f"round={round_pnl:+.2f} total={self.cumulative_pnl:.2f} KES"
                        )
                        self.log.info(
                            "ROUND %d | %s | round=%.2f KES | session=%.2f KES | total=%.2f KES",
                            self.total_rounds, desc, round_pnl, session_pnl, self.cumulative_pnl,
                        )
                        await self._read_balance()

                    # ── Decide what to do next ────────────────────────────────
                    if round_pnl > 0:
                        self.log.info(
                            "WIN this round (+%.2f KES) — returning to WATCH mode. "
                            "Session total: %.2f KES.  P1 deficit: %.2f  P2 deficit: %.2f KES.",
                            round_pnl, session_pnl,
                            self.recovery_deficit, self.p2_recovery_deficit,
                        )
                        bet_next, watching = False, True
                        session_pnl = 0.0
                        self._cooldown_rounds = self.BURST_COOLDOWN
                        try:
                            if self.p1_bet != self.BET_AMOUNT:
                                await self._set_panel1_bet(frame, self.BET_AMOUNT)
                                self.p1_bet = self.BET_AMOUNT
                            if self.p2_bet != self.P2_BET_AMOUNT:
                                await self._set_panel2_bet(frame, self.P2_BET_AMOUNT)
                                self.p2_bet = self.P2_BET_AMOUNT
                        except Exception:
                            pass

                    elif rounds_left <= 0:
                        self.log.info(
                            "All %d rounds used, no win. Session P&L = %.2f KES — "
                            "back to WATCH mode.  "
                            "P1 deficit: %.2f KES (next bet %.2f) | "
                            "P2 deficit: %.2f KES (next bet %.2f).",
                            self.MAX_BET_ROUNDS, session_pnl,
                            self.recovery_deficit, self._p1_bet(),
                            self.p2_recovery_deficit, self._p2_bet(),
                        )
                        bet_next, watching = False, True
                        session_pnl = 0.0
                        self._cooldown_rounds = self.BURST_COOLDOWN
                        try:
                            if self.p1_bet != self.BET_AMOUNT:
                                await self._set_panel1_bet(frame, self.BET_AMOUNT)
                                self.p1_bet = self.BET_AMOUNT
                            if self.p2_bet != self.P2_BET_AMOUNT:
                                await self._set_panel2_bet(frame, self.P2_BET_AMOUNT)
                                self.p2_bet = self.P2_BET_AMOUNT
                        except Exception:
                            pass

                    else:
                        self.log.info("Lost this round. %d round(s) left — betting next round.", rounds_left)
                        # bet_next stays True

                else:
                    # ── Watch round (no bet) ──────────────────────────────────
                    try:
                        prev_history = await get_crash_history(frame)
                        history = await wait_for_round_end(frame, prev_history)
                    except TimeoutError:
                        self.log.warning("Round end timeout during watch — retrying.")
                        continue
                    except Exception as e:
                        self.log.warning("Frame stale during watch (%s) — retrying.", e)
                        continue

                    crash_mult = history[0]
                    self.csv.record(crash_mult, mode="watch", cumulative_pnl=self.cumulative_pnl)

                    # ── Burst cooldown ────────────────────────────────────────
                    if self._cooldown_rounds > 0:
                        self._cooldown_rounds -= 1
                        self.last_event = (
                            f"Cooldown: {self._cooldown_rounds} round(s) left "
                            f"(crash={crash_mult:.2f}x)"
                        )
                        self.log.info(
                            "COOLDOWN: %d round(s) remaining — skipping trigger.",
                            self._cooldown_rounds,
                        )
                        continue

                    # ── Trigger conditions (respects TRIGGER_MODE) ───────────
                    trigger_high = (
                        self.TRIGGER_MODE in ("both", "high_only")
                        and crash_mult > self.TRIGGER_MULT
                    )
                    recent_n = history[:self.LOW_STREAK_ROUNDS]
                    trigger_low = (
                        self.TRIGGER_MODE in ("both", "low_only")
                        and len(recent_n) >= self.LOW_STREAK_ROUNDS
                        and all(m <= self.LOW_STREAK_MAX for m in recent_n)
                    )

                    if trigger_high:
                        trigger_reason = f"last crash {crash_mult:.2f}x > {self.TRIGGER_MULT:.1f}x"
                    elif trigger_low:
                        trigger_reason = (
                            f"last {self.LOW_STREAK_ROUNDS} crashes all ≤ {self.LOW_STREAK_MAX:.1f}x "
                            f"({[round(m,2) for m in recent_n]})"
                        )
                    else:
                        trigger_reason = None

                    self.last_event = f"Watching — last crash {crash_mult:.2f}x | total={self.cumulative_pnl:.2f} KES"
                    self.log.info(
                        "WATCH | crash=%.2fx | trigger_high=%s | trigger_low=%s | mode=%s",
                        crash_mult, trigger_high, trigger_low, self.TRIGGER_MODE,
                    )

                    if trigger_reason:
                        self.last_event = f"TRIGGER: {trigger_reason}"
                        self.log.info(
                            "TRIGGER HIT (%s) — betting next %d round(s)!",
                            trigger_reason, self.MAX_BET_ROUNDS,
                        )
                        bet_next          = True
                        rounds_left       = self.MAX_BET_ROUNDS
                        self._rounds_left = self.MAX_BET_ROUNDS
                        session_pnl       = 0.0

        except KeyboardInterrupt:
            self.log.info("Interrupted by user.")
        except Exception as e:
            self.log.exception("Unhandled error: %s", e)
        finally:
            self._print_summary()
            self.csv.close()
            if self.AUTO_LOGOUT:
                await self.logout()
            else:
                self.log.info("Auto-logout disabled — staying logged in.")
                self.last_event = "Bot stopped (still logged in)"
            await self.stop()

    def _print_summary(self):
        self.log.info("=" * 60)
        self.log.info("SESSION SUMMARY")
        self.log.info("  Rounds bet    : %d", self.total_rounds)
        self.log.info("  Wins          : %d", self.total_wins)
        self.log.info("  Losses        : %d", self.total_losses)
        rate = (self.total_wins / self.total_rounds * 100) if self.total_rounds else 0
        self.log.info("  Win rate      : %.1f%%", rate)
        self.log.info("  Net P&L       : KES %.2f", self.cumulative_pnl)
        self.log.info("=" * 60)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    await AviatorBot().run()


if __name__ == "__main__":
    asyncio.run(main())
