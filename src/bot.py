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
import re
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


def _parse_amount(value: str | None) -> Optional[float]:
    """Extract a numeric balance from free-form wallet text."""
    if not value:
        return None
    cleaned = value.replace("\xa0", " ").strip()
    matches = re.findall(r"\d+(?:[.,]\d+)?", cleaned)
    if not matches:
        return None
    token = matches[-1].replace(",", "")
    try:
        return float(token)
    except ValueError:
        return None

# ── Confirmed selectors (from inspector.py 2026-05-09) ────────────────────────
SEL = {
    # Login
    "login_user":   'input[name="user"]',
    "login_pass":   'input[name="password"]',
    "login_btn":    '[data-testid="login-form-submit-button"]',
    # Main page
    "cookie_accept": 'button.btn-primary',
    # Bet amount inputs — SportPesa uses placeholder="1", Spribe demo uses placeholder="0.1"
    "bet_inputs":   'input[placeholder="1"], input[placeholder="0.1"]',
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


def normalize_bet_pattern(raw_pattern, fallback_bets: int) -> list[bool]:
    """
    Convert a pattern definition into a list of booleans.

    Supported forms:
      - None -> [True] * fallback_bets
      - "0,1" / "skip,bet" / "101"
      - [0, 1] / [False, True]
    """
    if raw_pattern is None:
        pattern = [True] * max(0, int(fallback_bets))
    elif isinstance(raw_pattern, str):
        compact = raw_pattern.replace(" ", "")
        if compact and set(compact) <= {"0", "1"} and "," not in compact:
            tokens = list(compact)
        else:
            tokens = [tok.strip().lower() for tok in raw_pattern.split(",") if tok.strip()]
        pattern = []
        for tok in tokens:
            if tok in ("1", "true", "t", "bet", "b"):
                pattern.append(True)
            elif tok in ("0", "false", "f", "skip", "s"):
                pattern.append(False)
            else:
                raise ValueError(f"Unsupported bet pattern token: {tok!r}")
    else:
        pattern = [bool(int(v)) if isinstance(v, str) else bool(v) for v in list(raw_pattern)]
    if not pattern:
        raise ValueError("Bet pattern must contain at least one step.")
    if not any(pattern):
        raise ValueError("Bet pattern must contain at least one betting step.")
    return pattern


def format_bet_pattern(pattern: list[bool]) -> str:
    return " -> ".join("BET" if step else "SKIP" for step in pattern)


def next_pattern_state(pattern: list[bool]) -> str:
    if not pattern:
        return "watch"
    return "BET" if pattern[0] else "skip"


async def test_credentials(username: str, password: str, headless: bool = True, browser=None) -> dict:
    """
    Try to log in to SportPesa with the given credentials.
    Returns {"ok": bool, "message": str}.
    Pass `browser` to reuse a shared Playwright Browser (server mode).
    If `browser` is None, a temporary headless browser is launched.
    """
    pw = None
    own_browser = browser is None
    ctx = None
    try:
        if own_browser:
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
        if ctx:
            try:
                await ctx.close()
            except Exception:
                pass
        if own_browser and browser:
            try:
                await browser.close()
            except Exception:
                pass
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass


def calc_p1_bet(recovery_deficit: float) -> float:
    """
    P1 bet = round((deficit + RECOVERY_PROFIT_TARGET) / (PANEL1_CASHOUT - 1), 2).
    P2 always stays at 1 KES — only P1 scales.
    """
    if recovery_deficit <= 0:
        return 1.0
    net_multiplier = max(0.01, config.PANEL1_CASHOUT - 1)
    return max(1.0, round((recovery_deficit + config.RECOVERY_PROFIT_TARGET) / net_multiplier, 2))


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
    Appends every round to a CSV for AI training.

    Columns:
      timestamp  — ISO-8601 local time the round ended
      crash_mult — the multiplier at which the plane crashed (e.g. 3.45)
      total_win  — running cumulative P&L for the session (mirrors the
                   "Total Win" figure shown in the game's left sidebar)
      running_balance_after_bet — tracked balance text logged after each round
    """

    COLUMNS = ["timestamp", "crash_mult", "total_win", "running_balance_after_bet"]

    def __init__(
        self,
        session_id: str = "local",
        panel1_cashout: float = None,
        panel2_cashout: float = None,
        trigger_mult: float = None,
    ):
        # cashout/trigger kept as params so callers don't break
        os.makedirs("history", exist_ok=True)
        date_str = datetime.now().strftime("%Y%m%d")
        self.path = os.path.join("history", f"aviator_{date_str}_{session_id}.csv")
        write_header = not os.path.exists(self.path)
        if not write_header:
            try:
                with open(self.path, "r", newline="", encoding="utf-8") as existing_fh:
                    header = next(csv.reader(existing_fh), [])
                if header != self.COLUMNS:
                    self.path = os.path.join("history", f"aviator_{date_str}_{session_id}_v2.csv")
                    write_header = not os.path.exists(self.path)
            except Exception:
                self.path = os.path.join("history", f"aviator_{date_str}_{session_id}_v2.csv")
                write_header = not os.path.exists(self.path)
        self._fh  = open(self.path, "a", newline="", encoding="utf-8")
        self._csv = csv.DictWriter(self._fh, fieldnames=self.COLUMNS)
        if write_header:
            self._csv.writeheader()
        log.info("History CSV: %s", os.path.abspath(self.path))

    def record(
        self,
        crash_mult: float,
        total_win: float = 0.0,
        running_balance_after_bet: str = "",
        **_ignored,
    ):
        self._csv.writerow({
            "timestamp":                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "crash_mult":                f"{crash_mult:.2f}",
            "total_win":                 f"{total_win:.2f}",
            "running_balance_after_bet": running_balance_after_bet,
        })
        self._fh.flush()

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
        shared_browser=None,
    ):
        self._username   = username   or config.USERNAME
        self._password   = password   or config.PASSWORD
        self._headless   = headless   if headless is not None else config.HEADLESS
        self._session_id = session_id or "local"
        self.DEMO_MODE   = demo_mode
        self.AUTO_LOGOUT = auto_logout
        self._shared_browser = shared_browser   # server passes its shared Browser instance
        self._pw = None                          # only set when we own the playwright instance

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
        self.P1_TRIGGER_MULT     = s.get("p1_trigger_mult",     config.P1_TRIGGER_MULT)
        self.P1_TRIGGER_MULT_MAX = s.get("p1_trigger_mult_max", getattr(config, "P1_TRIGGER_MULT_MAX", float("inf")))
        self.P1_LOW_STREAK_MAX   = s.get("p1_low_streak_max",   config.P1_LOW_STREAK_MAX)
        self.P1_LOW_STREAK_COUNT = s.get("p1_low_streak_count", config.P1_LOW_STREAK_COUNT)
        self.P1_MAX_BET_ROUNDS   = s.get("p1_max_bet_rounds",   config.P1_MAX_BET_ROUNDS)
        self.P1_BET_PATTERN      = normalize_bet_pattern(
            s.get("p1_bet_pattern", getattr(config, "P1_BET_PATTERN", None)),
            self.P1_MAX_BET_ROUNDS,
        )
        self.P2_TRIGGER_MULT     = s.get("p2_trigger_mult",     config.P2_TRIGGER_MULT)
        self.P2_TRIGGER_MULT_MAX = s.get("p2_trigger_mult_max", getattr(config, "P2_TRIGGER_MULT_MAX", float("inf")))
        self.P2_LOW_STREAK_MIN   = s.get("p2_low_streak_min",   getattr(config, "P2_LOW_STREAK_MIN", 0.0))
        self.P2_LOW_STREAK_MAX   = s.get("p2_low_streak_max",   config.P2_LOW_STREAK_MAX)
        self.P2_LOW_STREAK_COUNT = s.get("p2_low_streak_count", config.P2_LOW_STREAK_COUNT)
        self.P2_MAX_BET_ROUNDS   = s.get("p2_max_bet_rounds",   config.P2_MAX_BET_ROUNDS)
        self.P2_BET_PATTERN      = normalize_bet_pattern(
            s.get("p2_bet_pattern", getattr(config, "P2_BET_PATTERN", None)),
            self.P2_MAX_BET_ROUNDS,
        )
        self.RECOVERY_PROFIT_TARGET  = s.get("recovery_profit_target", config.RECOVERY_PROFIT_TARGET)
        self.STOP_ON_PROFIT          = s.get("stop_on_profit",         config.STOP_ON_PROFIT)
        self.STOP_ON_LOSS            = s.get("stop_on_loss",           config.STOP_ON_LOSS)
        self.BET_AMOUNT              = s.get("bet_amount",             config.BET_AMOUNT)
        self.LOW_STREAK_ROUNDS          = s.get("low_streak_rounds",          8)
        self.TRIGGER_MODE               = s.get("trigger_mode",               "both")
        self.RECOVERY_ENABLED           = s.get("recovery_enabled",           config.RECOVERY_ENABLED)
        self.RECOVERY_SCOPE             = s.get("recovery_scope",             config.RECOVERY_SCOPE)
        self.RECOVERY_PERCENTAGE        = s.get("recovery_percentage",        config.RECOVERY_PERCENTAGE)
        self.BURST_COOLDOWN             = s.get("burst_cooldown",             0)
        self.STOP_ON_CONSECUTIVE_LOSSES = s.get("stop_on_consecutive_losses", 0)
        # Panel 2 independent recovery
        self.P2_BET_AMOUNT             = s.get("p2_bet_amount",             self.BET_AMOUNT)
        self.P2_RECOVERY_ENABLED       = s.get("p2_recovery_enabled",       config.P2_RECOVERY_ENABLED)
        self.P2_RECOVERY_PROFIT_TARGET = s.get("p2_recovery_profit_target", self.RECOVERY_PROFIT_TARGET)
        self.P2_RECOVERY_SCOPE         = s.get("p2_recovery_scope",         config.P2_RECOVERY_SCOPE)
        self.P2_RECOVERY_PERCENTAGE    = s.get("p2_recovery_percentage",    config.P2_RECOVERY_PERCENTAGE)
        self.RECOVERY_STEPS            = s.get("recovery_steps",            config.RECOVERY_STEPS)
        self.P2_RECOVERY_STEPS         = s.get("p2_recovery_steps",         config.P2_RECOVERY_STEPS)
        self.P1_ASSIST_P2_ENABLED      = s.get("p1_assist_p2_enabled",      config.P1_ASSIST_P2_ENABLED)
        self.P1_ASSIST_PERCENTAGE      = s.get("p1_assist_percentage",      config.P1_ASSIST_PERCENTAGE)
        self.P1_ASSIST_TRIGGER_MAX     = s.get("p1_assist_trigger_max",     getattr(config, "P1_ASSIST_TRIGGER_MAX", 1.4))
        self.P1_ASSIST_CASHOUT         = s.get("p1_assist_cashout",         getattr(config, "P1_ASSIST_CASHOUT", 1.4))
        self.P2_ASSIST_P1_ENABLED      = s.get("p2_assist_p1_enabled",      config.P2_ASSIST_P1_ENABLED)
        self.P2_ASSIST_PERCENTAGE      = s.get("p2_assist_percentage",       config.P2_ASSIST_PERCENTAGE)
        # Recovery guardrails
        self.MAX_RECOVERY_BET      = s.get("max_recovery_bet",      getattr(config, "MAX_RECOVERY_BET",      0))
        self.MAX_ASSIST_BET        = s.get("max_assist_bet",        getattr(config, "MAX_ASSIST_BET",        0))
        self.MAX_P2_BET            = s.get("max_p2_bet",            getattr(config, "MAX_P2_BET",            0))
        self.RECOVERY_DEFICIT_CAP  = s.get("recovery_deficit_cap",  getattr(config, "RECOVERY_DEFICIT_CAP",  0))
        self.TRIGGER_LOSS_COOLDOWN = s.get("trigger_loss_cooldown", getattr(config, "TRIGGER_LOSS_COOLDOWN", 0))

        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page:    Optional[Page]    = None

        self.total_rounds = 0
        self.total_wins   = 0
        self.total_losses = 0
        self.cumulative_pnl = 0.0

        initial_demo_balance = getattr(config, "INITIAL_DEMO_BALANCE", None)
        self.recovery_deficit    = 0.0
        self.p2_recovery_deficit = 0.0
        self.p1_bet = self.BET_AMOUNT
        self.p2_bet = self.P2_BET_AMOUNT
        self.last_event = "idle"
        self.account_balance = "—"
        self.browser_phase = "idle"
        self._demo_bankroll_base: Optional[float] = (
            float(initial_demo_balance)
            if self.DEMO_MODE and initial_demo_balance not in (None, 0, 0.0, "")
            else None
        )
        self._demo_last_raw_balance: Optional[str] = None

        if self._demo_bankroll_base is not None:
            self.account_balance = self._format_demo_balance()

        self._p1_cooldown           = 0
        self._p2_cooldown           = 0
        self._p1_consecutive_losses = 0
        self._p2_consecutive_losses = 0
        self._rounds_left           = 0   # kept for legacy bet-sizing compat
        self._p1_step            = 0   # persistent pct-recovery step for P1 (carries across bursts)
        self._p2_step            = 0   # persistent pct-recovery step for P2
        self._demo_reconnects    = 0   # how many times we have reopened the demo tab

        self.csv = HistoryCSV(
            session_id=self._session_id,
            panel1_cashout=self.PANEL1_CASHOUT,
            panel2_cashout=self.PANEL2_CASHOUT,
            trigger_mult=self.P1_TRIGGER_MULT,
        )

    def _tracked_demo_balance(self) -> Optional[float]:
        if self._demo_bankroll_base is None:
            return None
        return round(self._demo_bankroll_base + self.cumulative_pnl, 2)

    def _format_demo_balance(self) -> str:
        tracked = self._tracked_demo_balance()
        if tracked is None:
            return self.account_balance
        return f"Demo: {tracked:,.2f} KES"

    def _status_snapshot(self) -> str:
        balance = self.account_balance or "—"
        return (
            f"balance={balance} | pnl={self.cumulative_pnl:+.2f} KES | "
            f"p1_def={self.recovery_deficit:.2f} | p2_def={self.p2_recovery_deficit:.2f} | "
            f"wins={self.total_wins} losses={self.total_losses} | reconnects={self._demo_reconnects}"
        )

    def _log_status_snapshot(self, label: str):
        self.log.info("%s | %s", label, self._status_snapshot())

    def _set_phase(self, phase: str, message: str | None = None):
        self.browser_phase = phase
        if message is not None:
            self.last_event = message

    def browser_status_text(self) -> str:
        mode = "headless" if self._headless else "visible"
        demo = "demo" if self.DEMO_MODE else "real"
        runtime = "connected" if self._runtime_alive() else "not ready"
        return f"{mode} browser | {demo} mode | runtime {runtime}"

    def _running_balance_text(self) -> str:
        tracked_demo = self._tracked_demo_balance()
        if tracked_demo is not None:
            return f"{tracked_demo:,.2f} KES"
        if self.account_balance and self.account_balance != "—":
            return self.account_balance
        return f"P&L {self.cumulative_pnl:+.2f} KES"

    def _runtime_alive(self) -> bool:
        if not self.browser or not self.context or not self.page:
            return False
        try:
            if not self.browser.is_connected():
                return False
        except Exception:
            return False
        try:
            if self.page.is_closed():
                return False
        except Exception:
            return False
        return True

    def _p1_bet(self, extra_risk: float = 0.0) -> float:
        if not self.RECOVERY_ENABLED:
            return self.BET_AMOUNT
        p1d = self.recovery_deficit
        p2d = self.p2_recovery_deficit
        if self.RECOVERY_SCOPE == "individual":
            if p1d > 0:
                target = p1d
            elif self.P1_ASSIST_P2_ENABLED and p2d > 0:
                target = p2d * self.P1_ASSIST_PERCENTAGE / 100
            else:
                target = 0.0
        elif self.RECOVERY_SCOPE in ("combined", "smart"):
            target = p1d + p2d   # P1 is the big gun — covers everything
        else:  # "percentage"
            total = p1d + p2d
            max_steps = self.RECOVERY_STEPS if self.RECOVERY_STEPS > 0 else self.P1_MAX_BET_ROUNDS
            is_last = (self._p1_step + 1) >= max_steps
            target = total if is_last else total * self.RECOVERY_PERCENTAGE / 100
        if target <= 0:
            return self.BET_AMOUNT
        net_multiplier = max(0.01, self.PANEL1_CASHOUT - 1)
        bet = max(self.BET_AMOUNT,
                  round((target + extra_risk + self.RECOVERY_PROFIT_TARGET) / net_multiplier, 2))
        cap = self.MAX_RECOVERY_BET
        return min(bet, cap) if cap > 0 else bet

    def _p2_bet(self, extra_risk: float = 0.0) -> float:
        if not self.P2_RECOVERY_ENABLED:
            return self.P2_BET_AMOUNT
        p1d = self.recovery_deficit
        p2d = self.p2_recovery_deficit
        if p1d > 0 and self.P2_ASSIST_P1_ENABLED:
            assist_target = p1d * self.P2_ASSIST_PERCENTAGE / 100
            net_multiplier = max(0.01, self.PANEL2_CASHOUT - 1)
            return max(self.P2_BET_AMOUNT,
                       round((assist_target + extra_risk + self.P2_RECOVERY_PROFIT_TARGET) / net_multiplier, 2))
        if self.P2_RECOVERY_SCOPE in ("individual", "smart"):
            target = p2d
        elif self.P2_RECOVERY_SCOPE == "combined":
            target = p1d + p2d
        else:  # "percentage"
            total = p1d + p2d
            max_steps = self.P2_RECOVERY_STEPS if self.P2_RECOVERY_STEPS > 0 else self.P2_MAX_BET_ROUNDS
            is_last = (self._p2_step + 1) >= max_steps
            target = total if is_last else total * self.P2_RECOVERY_PERCENTAGE / 100
        if target <= 0:
            return self.P2_BET_AMOUNT
        net_multiplier = max(0.01, self.PANEL2_CASHOUT - 1)
        bet = max(self.P2_BET_AMOUNT,
                  round((target + extra_risk + self.P2_RECOVERY_PROFIT_TARGET) / net_multiplier, 2))
        cap = self.MAX_P2_BET
        return min(bet, cap) if cap > 0 else bet

    def _p1_assist_p2_bet(self) -> float:
        if not self.P1_ASSIST_P2_ENABLED or self.p2_recovery_deficit <= 0:
            return self.BET_AMOUNT
        target = self.p2_recovery_deficit * self.P1_ASSIST_PERCENTAGE / 100
        net_multiplier = max(0.01, self.P1_ASSIST_CASHOUT - 1)
        bet = max(self.BET_AMOUNT, round((target + self.RECOVERY_PROFIT_TARGET) / net_multiplier, 2))
        cap = self.MAX_ASSIST_BET
        return min(bet, cap) if cap > 0 else bet

    def _round_pnl(
        self,
        crash_mult: float,
        p1_bet: float,
        p2_bet: float,
        p1_cashout: float = None,
        p2_cashout: float = None,
    ) -> tuple[float, str]:
        p1_cashout = self.PANEL1_CASHOUT if p1_cashout is None else p1_cashout
        p2_cashout = self.PANEL2_CASHOUT if p2_cashout is None else p2_cashout
        p1_win = crash_mult >= p1_cashout
        p2_win = crash_mult >= p2_cashout
        pnl = 0.0
        pnl += p1_bet * (p1_cashout - 1) if p1_win else -p1_bet
        pnl += p2_bet * (p2_cashout - 1) if p2_win else -p2_bet
        p1_tag = f"WIN@{p1_cashout:.1f}x" if p1_win else "LOSS"
        p2_tag = f"WIN@{p2_cashout:.1f}x" if p2_win else "LOSS"
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

        self._set_phase("watching", f"AI: collecting history (0/{self.AI_HISTORY_WINDOW})")
        self.log.info("=" * 60)
        self.log.info("AI strategy active — history window: %d rounds", self.AI_HISTORY_WINDOW)
        self.log.info("  Baseline  P1: %.2f KES @ %.1fx | P2: %.2f KES @ %.1fx",
                      self.BET_AMOUNT, self.PANEL1_CASHOUT, self.P2_BET_AMOUNT, self.PANEL2_CASHOUT)
        self.log.info("=" * 60)
        self._log_status_snapshot("AI START")

        while True:
            if self._stop_event.is_set():
                self._set_phase("stopping", "stopped")
                break
            reason = self.should_stop()
            if reason:
                self._set_phase("stopping", reason)
                self.log.info("AI: stopping — %s", reason)
                break
            if not self._runtime_alive():
                try:
                    frame = await self._recover_runtime("browser/page not alive")
                    continue
                except Exception as e:
                    self.log.error("AI: runtime recovery failed (%s) — aborting.", e)
                    break

            frame = self._get_frame()
            if frame is None:
                self.log.warning("AI: game frame lost — waiting…")
                try:
                    frame = await self._wait_for_frame(timeout_s=30)
                except TimeoutError:
                    if self.DEMO_MODE:
                        try:
                            frame = await self._reconnect_demo()
                            continue
                        except Exception as e:
                            self.log.error("AI: reconnect failed (%s) — aborting.", e)
                            break
                    self.log.error("AI: frame never came back — aborting.")
                    break

            self._set_phase("watching", "AI: waiting for next round…")
            try:
                ok = await wait_for_bet_phase(frame, timeout_s=2)
            except Exception as e:
                self.log.warning("AI: frame lost during bet-phase wait (%s).", e)
                if self.DEMO_MODE:
                    try:
                        frame = await self._reconnect_demo()
                    except Exception as re:
                        self.log.error("AI: reconnect failed (%s) — aborting.", re)
                        break
                continue
            if not ok:
                if not self._runtime_alive():
                    try:
                        frame = await self._recover_runtime("browser/page closed during bet wait")
                        continue
                    except Exception as e:
                        self.log.error("AI: runtime recovery failed (%s) — aborting.", e)
                        break
                if self.DEMO_MODE:
                    continue
                self.log.error("AI: bet phase never opened — aborting.")
                break

            # ── Not enough history yet — watch without betting ────────────────
            if len(history) < self.AI_HISTORY_WINDOW:
                self._set_phase("watching", f"AI: collecting history ({len(history)}/{self.AI_HISTORY_WINDOW})")
                self.log.info("AI: history %d/%d — watching.", len(history), self.AI_HISTORY_WINDOW)
                try:
                    prev    = await get_crash_history(frame)
                    history = await wait_for_round_end(frame, prev)
                except Exception as e:
                    self.log.warning("AI: frame stale collecting history (%s).", e)
                    if self.DEMO_MODE:
                        try:
                            frame = await self._reconnect_demo()
                        except Exception as re:
                            self.log.error("AI: reconnect failed (%s) — aborting.", re)
                            break
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
            self._set_phase("betting", (
                f"AI betting — P1={p1_bet:.2f}@{p1_cashout:.1f}x  "
                f"P2={p2_bet:.2f}@{p2_cashout:.1f}x"
            ))
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
                if self.DEMO_MODE:
                    try:
                        frame = await self._reconnect_demo()
                    except Exception as re:
                        self.log.error("AI: reconnect failed (%s) — aborting.", re)
                        break
                continue

            if not placed:
                self.log.warning("AI: could not place bets — skipping round.")
                try:
                    prev2   = await get_crash_history(frame)
                    history = await wait_for_round_end(frame, prev2)
                except Exception:
                    if self.DEMO_MODE:
                        try:
                            frame = await self._reconnect_demo()
                        except Exception:
                            pass
                continue

            # ── Wait for round end ────────────────────────────────────────────
            try:
                history = await wait_for_round_end(frame, prev)
            except TimeoutError:
                if self.DEMO_MODE:
                    self.log.warning("AI: round end timeout — reconnecting demo.")
                    try:
                        frame = await self._reconnect_demo()
                        continue
                    except Exception as e:
                        self.log.error("AI: reconnect failed (%s) — aborting.", e)
                        break
                self.log.error("AI: round end timeout — continuing.")
                continue
            except Exception as e:
                self.log.warning("AI: frame stale waiting for round end (%s).", e)
                if self.DEMO_MODE:
                    try:
                        frame = await self._reconnect_demo()
                    except Exception as re:
                        self.log.error("AI: reconnect failed (%s) — aborting.", re)
                        break
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

            self._set_phase("round_complete", (
                f"AI Round {self.total_rounds}: crash={crash_mult:.2f}x "
                f"round={round_pnl:+.2f} total={self.cumulative_pnl:.2f} KES"
            ))
            self.log.info(
                "AI ROUND %d | %s | round=%+.2f KES | total=%.2f KES",
                self.total_rounds, desc, round_pnl, self.cumulative_pnl,
            )
            await self._read_balance()
            self.csv.record(
                crash_mult,
                total_win=self.cumulative_pnl,
                running_balance_after_bet=self._running_balance_text(),
            )
            self.log.info("RUNNING BALANCE AFTER BET: %s", self._running_balance_text())
            self._log_status_snapshot(f"AI ROUND {self.total_rounds}")

    # ── Browser ───────────────────────────────────────────────────────────────

    async def start(self):
        if self._shared_browser is not None:
            # Server mode: reuse the shared Chrome process — just create an isolated context.
            self._set_phase("launching", "Creating isolated browser context…")
            self.browser = self._shared_browser
        else:
            # Standalone mode: launch our own Chrome process.
            self._set_phase("launching", f"Launching {'headless' if self._headless else 'visible'} browser…")
            self._pw = await async_playwright().start()
            self.browser = await self._pw.chromium.launch(
                headless=self._headless,
                slow_mo=config.SLOW_MO,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
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
        self._set_phase("browser_ready", "Browser ready — opening session")

    async def logout(self):
        """Log out of SportPesa before closing the browser."""
        if not self.page:
            return
        try:
            self._set_phase("logging_out", "Logging out…")
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
            self._set_phase("stopped", "Logged out")
            self.log.info("Logout complete.")
        except Exception as e:
            self.log.warning("Logout attempt failed: %s", e)

    async def stop(self):
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        if self._shared_browser is None and self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass

    # ── Login ─────────────────────────────────────────────────────────────────

    async def login(self):
        self._set_phase("logging_in", "Logging in…")
        self.log.info("Logging in…")
        await self.page.goto(config.LOGIN_URL, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(2000)
        await self.page.fill(SEL["login_user"], self._username)
        await self.page.fill(SEL["login_pass"], self._password)
        await self.page.click(SEL["login_btn"])
        try:
            await self.page.wait_for_url(lambda u: "login" not in u, timeout=15_000)
            self._set_phase("logged_in", "Login successful")
            self.log.info("Login successful.")
            await self._dismiss_page_popups()
            await self._read_balance()
        except PWTimeout:
            self._set_phase("error", "Login failed — check credentials")
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
                        self._demo_last_raw_balance = balance
                        amount = _parse_amount(balance)
                        if amount is not None and self._demo_bankroll_base is None:
                            # If config did not provide a demo bankroll, lock to the
                            # first seen wallet and let cumulative_pnl drive updates.
                            self._demo_bankroll_base = round(amount - self.cumulative_pnl, 2)
                            self.log.info("Demo bankroll base set to %.2f KES.", self._demo_bankroll_base)
                        if self._demo_bankroll_base is not None:
                            self.account_balance = self._format_demo_balance()
                            self.log.info(
                                "Demo balance tracked: %s (raw UI: %s)",
                                self.account_balance,
                                balance,
                            )
                            self._log_status_snapshot("DEMO WALLET")
                        else:
                            self.account_balance = balance
                            self.log.info("Demo balance: %s", balance)
                            self._log_status_snapshot("DEMO WALLET")
                    else:
                        if self._demo_bankroll_base is not None:
                            self.account_balance = self._format_demo_balance()
                        self.log.debug("Demo balance not found in frame yet.")
                elif self._demo_bankroll_base is not None:
                    self.account_balance = self._format_demo_balance()
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
                self._log_status_snapshot("ACCOUNT")
            else:
                self.log.warning("Balance not found — page may still be loading or selector changed")
        except Exception as e:
            self.log.debug("Balance read failed: %s", e)

    # ── Popup & mode handling ─────────────────────────────────────────────────

    async def _dismiss_page_popups(self):
        """Close SportPesa modals — deposit tooltips, cookie prompts, etc."""
        await asyncio.sleep(1.0)
        POPUP_SELS = [
            # Deposit tooltip "OK" link (appears immediately on low balance)
            'a.custom-tooltip__close',
            # Quick Deposit modal close buttons
            '.quick-deposit-modal .btn-close',
            '.quick-deposit-modal .close',
            '.quick-deposit .close',
            '.deposit-modal .close',
            '[class*="quick-deposit"] button.close',
            # Bootstrap modal close buttons
            '.modal.show .btn-close',
            '.modal.show .close',
            '.modal.show button[data-dismiss="modal"]',
            '.modal.show [aria-label="Close"]',
            # Generic dismiss patterns
            'button[data-dismiss="modal"]',
            '.modal__close', '.dialog__close', '.popup__close',
            '[aria-label="Close"]', '[aria-label="close"]',
        ]
        for _pass in range(2):
            for sel in POPUP_SELS:
                try:
                    el = await self.page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        await asyncio.sleep(0.4)
                        self.log.info("Dismissed popup: %s", sel)
                        break
                except Exception:
                    continue
            try:
                await self.page.keyboard.press("Escape")
                await asyncio.sleep(0.3)
            except Exception:
                pass

    def _get_casino_frame(self):
        """Return the casino-frontend iframe (hosts game cards + Demo/Play buttons)."""
        for f in self.page.frames:
            if "casino-frontend" in f.url:
                return f
        return None

    async def _click_casino_demo_button(self):
        """
        Click the Demo button inside the casino-frontend.ke.sportpesa.com iframe.
        When navigating to the Aviator URL the casino-frontend shows the Aviator
        card with Demo/Play buttons already visible — no hover required.
        Must be called quickly (within ~3 s of page load) before the low-balance
        redirect fires.  Returns True if Demo was clicked.
        """
        for attempt in range(6):          # retry for up to ~3 s
            frame = self._get_casino_frame()
            if frame:
                try:
                    el = await frame.query_selector('button:has-text("Demo")')
                    if el and await el.is_visible():
                        await el.click()
                        await asyncio.sleep(1.5)
                        self.log.info("Demo clicked in casino-frontend frame (attempt %d).", attempt + 1)
                        return True
                except Exception as exc:
                    self.log.debug("Demo click attempt %d: %s", attempt + 1, exc)
            await asyncio.sleep(0.5)
        self.log.info("Demo button not found in casino-frontend frame — Spribe fallback will run.")
        return False

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
        Return the live Spribe game frame.
        Demo mode: self.page IS the spribegaming tab — main frame is the game.
        SportPesa mode: game lives inside an iframe in the casino-frontend.
        Never cache the result — the iframe reloads periodically.
        """
        # Demo mode: self.page IS the spribegaming tab
        if "spribegaming.com" in self.page.url or "aviator-next" in self.page.url:
            return self.page.main_frame
        # SportPesa mode: game runs inside an iframe
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

    async def open_aviator_demo(self):
        """
        Open the Spribe demo site (no login needed).
        Flow: spribe.co/games/aviator → Play Demo → Yes I'm over 18
              → game opens in a new tab at aviator-demo.spribegaming.com
        Swaps self.page to the new game tab and returns its main frame.
        """
        self._set_phase("opening_demo", "Opening Spribe demo…")
        self.log.info("Opening Spribe demo (no login required)…")
        demo_page = await self.context.new_page()

        new_tabs: list = []
        self.context.on("page", lambda pg: new_tabs.append(pg))

        await demo_page.goto("https://spribe.co/games/aviator", wait_until="domcontentloaded")
        await demo_page.wait_for_timeout(2000)

        try:
            await demo_page.click("button:has-text('Got it')", timeout=3000)
            self.log.info("Cookie banner dismissed.")
        except Exception:
            pass

        await demo_page.click('a.demo-link button, button.btn-demo', timeout=10_000)
        self.log.info("Play Demo clicked.")
        await demo_page.wait_for_timeout(1000)

        await demo_page.click("button:has-text('Yes')", timeout=8_000)
        self.log.info("Age confirmed.")

        self._set_phase("opening_demo", "Waiting for demo tab…")
        self.log.info("Waiting for demo game tab (spribegaming.com) to open…")
        game_tab = None
        for _ in range(40):
            for tab in new_tabs:
                if "spribegaming.com" in tab.url or "aviator-demo" in tab.url:
                    game_tab = tab
                    break
            if game_tab:
                break
            await asyncio.sleep(0.5)

        if not game_tab:
            raise TimeoutError("Demo game tab (spribegaming.com) did not open after 20 s")

        await game_tab.wait_for_load_state("domcontentloaded")
        self.log.info("Demo tab: %s", game_tab.url[:90])

        await demo_page.close()
        self.page = game_tab

        self._set_phase("loading_game", "Waiting for demo game inputs…")
        self.log.info("Waiting for demo game inputs…")
        frame = await self._wait_for_frame(timeout_s=45)
        await self._read_balance()
        self._set_phase("ready", "Demo game ready")
        self.log.info("Demo game ready.")
        return frame

    async def _reconnect_demo(self):
        """
        Called when the demo tab drops or freezes.
        Closes the stale tab, reopens a fresh Spribe demo session, re-sets
        up panels, and returns the new frame.  All deficit/PnL state is kept.
        """
        self._demo_reconnects += 1
        self._set_phase("recovering", f"Reconnecting to demo (attempt {self._demo_reconnects})…")
        self.log.warning("Demo connection lost — reconnecting (attempt %d)…", self._demo_reconnects)
        try:
            await self.page.close()
        except Exception:
            pass
        frame = await self.open_aviator_demo()
        await self.setup_panels(frame)
        await self._read_balance()
        self.log.info("Reconnected. Deficits preserved — P1=%.2f  P2=%.2f",
                      self.recovery_deficit, self.p2_recovery_deficit)
        self._log_status_snapshot("DEMO RECONNECTED")
        return frame

    async def _recover_runtime(self, reason: str = "runtime not alive"):
        """
        Recreate the browser/game runtime and continue with the same bot state.
        Keeps bankroll/PnL/deficits/CSV intact while rebuilding the page/frame.
        """
        self._set_phase("recovering", f"Recovering session ({reason})…")
        self.log.warning("Runtime recovery started: %s", reason)

        try:
            if self.page and not self.page.is_closed():
                await self.page.close()
        except Exception:
            pass
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass

        self.browser = None
        self.context = None
        self.page = None

        await self.start()
        if self.DEMO_MODE:
            frame = await self.open_aviator_demo()
        else:
            await self.login()
            frame = await self.open_aviator()
        await self.setup_panels(frame)
        await self._read_balance()
        self._demo_reconnects += 1
        self.log.info("Runtime recovered successfully.")
        self._log_status_snapshot("RUNTIME RECOVERED")
        return frame

    async def open_aviator(self):
        self._set_phase("opening_game", "Opening Aviator page…")
        self.log.info("Opening Aviator…")
        await self.page.goto(config.AVIATOR_URL, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(2000)
        try:
            await self.page.click(SEL["cookie_accept"], timeout=4_000)
            self.log.info("Cookie banner dismissed.")
        except PWTimeout:
            pass
        await self._dismiss_page_popups()
        self._set_phase("loading_game", "Waiting for game to load…")
        self.log.info("Waiting for Spribe game frame + inputs…")
        frame = await self._wait_for_frame(timeout_s=45)
        self._set_phase("loading_game", "Game loaded — setting up panels")
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
        bet_inputs = await frame.query_selector_all(SEL["bet_inputs"])
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
        bet_inputs = await frame.query_selector_all(SEL["bet_inputs"])
        if bet_inputs:
            await set_input(bet_inputs[0], amount)
            self.log.info("P1 bet → %.2f KES (P1 deficit: %.2f KES).", amount, self.recovery_deficit)

    async def _set_panel2_bet(self, frame, amount: float):
        bet_inputs = await frame.query_selector_all(SEL["bet_inputs"])
        if len(bet_inputs) > 1:
            await set_input(bet_inputs[1], amount)
            self.log.info("P2 bet → %.2f KES (P2 deficit: %.2f KES).", amount, self.p2_recovery_deficit)

    # ── Place bets on both panels ─────────────────────────────────────────────

    async def place_bets(self, frame, p1: bool = True, p2: bool = True) -> bool:
        btns = await frame.query_selector_all(SEL["bet_btn"])
        if not btns:
            self.log.warning("BET buttons not found — bet phase may have already closed.")
            return False
        placed = False
        if p1 and len(btns) > 0:
            await btns[0].click()
            placed = True
        if p2 and len(btns) > 1:
            await asyncio.sleep(0.1)
            await btns[1].click()
            placed = True
        self.log.info("Bets placed — P1=%s P2=%s.", p1, p2)
        return placed

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
            if self.DEMO_MODE:
                frame = await self.open_aviator_demo()   # no login needed
            else:
                await self.login()
                frame = await self.open_aviator()

            if self._strategy_type == "ai":
                await self.run_ai()
                return   # finally block still runs (summary / logout / stop)

            await self.setup_panels(frame)

            # ── Per-panel independent state ───────────────────────────────────
            p1_bet_plan    = []
            p1_assist_plan = []
            p1_session_pnl = 0.0

            p2_bet_plan    = []
            p2_session_pnl = 0.0

            history = await get_crash_history(frame)

            self._set_phase("watching", "Strategy active — watching for trigger")
            self.log.info("=" * 60)
            self.log.info("Strategy active — INDEPENDENT TRIGGERS")
            self.log.info("  P1: trigger > %.1fx | low ≤%.1fx × %d | pattern %s | cashout %.1fx",
                          self.P1_TRIGGER_MULT, self.P1_LOW_STREAK_MAX,
                          self.P1_LOW_STREAK_COUNT, format_bet_pattern(self.P1_BET_PATTERN), self.PANEL1_CASHOUT)
            self.log.info("  P2: trigger %.1fx < crash < %.1fx × %d | pattern %s | cashout %.1fx",
                          self.P2_LOW_STREAK_MIN, self.P2_LOW_STREAK_MAX,
                          self.P2_LOW_STREAK_COUNT, format_bet_pattern(self.P2_BET_PATTERN), self.PANEL2_CASHOUT)
            self.log.info("  P1 assist: P2 deficit + previous crash ≤ %.1fx | target %.0f%% | cashout %.1fx",
                          self.P1_ASSIST_TRIGGER_MAX, self.P1_ASSIST_PERCENTAGE, self.P1_ASSIST_CASHOUT)
            self.log.info("  P1 recovery=%s | P2 recovery=%s",
                          "ON" if self.RECOVERY_ENABLED else "OFF",
                          "ON" if self.P2_RECOVERY_ENABLED else "OFF")
            self.log.info("  Stop profit KES %.0f | Stop loss KES %.0f", self.STOP_ON_PROFIT, self.STOP_ON_LOSS)
            self.log.info("=" * 60)
            self._log_status_snapshot("BOT START")

            while True:
                # Stop if requested remotely or by profit/loss guard
                if self._stop_event.is_set():
                    self.log.info("Stop requested — exiting.")
                    self._set_phase("stopping", "stopped")
                    break

                reason = self.should_stop()
                if reason:
                    self.log.info("Bot stopping: %s", reason)
                    self._set_phase("stopping", reason)
                    break
                if not self._runtime_alive():
                    try:
                        frame = await self._recover_runtime("browser/page not alive")
                        continue
                    except Exception as e:
                        self.log.error("Runtime recovery failed: %s — aborting.", e)
                        break

                # Always use a fresh frame reference — the iframe reloads periodically
                frame = self._get_frame()
                if frame is None:
                    self.log.warning("Game frame lost — waiting for it to reload…")
                    try:
                        frame = await self._wait_for_frame(timeout_s=15)
                        self.log.info("Frame recovered.")
                    except TimeoutError:
                        if self.DEMO_MODE:
                            try:
                                frame = await self._reconnect_demo()
                            except Exception as e:
                                self.log.error("Reconnect failed: %s — aborting.", e)
                                break
                        else:
                            self.log.error("Frame never came back — aborting.")
                            break

                # Wait for the betting window to open
                self._set_phase("watching", f"Waiting for next round… [P1={next_pattern_state(p1_bet_plan)} P2={next_pattern_state(p2_bet_plan)}]")
                self.log.info("Waiting for bet phase… [P1=%s P2=%s]",
                              next_pattern_state(p1_bet_plan),
                              next_pattern_state(p2_bet_plan))
                try:
                    ok = await wait_for_bet_phase(frame, timeout_s=2)
                except Exception as e:
                    self.log.warning("Frame context lost during bet-phase wait (%s) — reconnecting.", e)
                    if self.DEMO_MODE:
                        try:
                            frame = await self._reconnect_demo()
                            continue
                        except Exception as re:
                            self.log.error("Reconnect failed: %s — aborting.", re)
                            break
                    continue
                if not ok:
                    if not self._runtime_alive():
                        try:
                            frame = await self._recover_runtime("browser/page closed during bet wait")
                            continue
                        except Exception as e:
                            self.log.error("Runtime recovery failed: %s — aborting.", e)
                            break
                    if self.DEMO_MODE:
                        continue
                    self.log.error("Bet phase never opened — aborting.")
                    break

                # Snapshot which panels are betting this round.
                # Clean panels can assist the panel carrying debt, using the
                # configured assist percentage instead of taking over all debt.
                p1_scheduled_this = p1_bet_plan.pop(0) if p1_bet_plan else False
                p1_low_assist_this = p1_assist_plan.pop(0) if p1_assist_plan else False
                p2_scheduled_this = p2_bet_plan.pop(0) if p2_bet_plan else False
                p2_assist_this = (
                    p1_scheduled_this
                    and self.recovery_deficit > 0
                    and self.P2_ASSIST_P1_ENABLED
                    and self.P2_RECOVERY_ENABLED
                )
                p1_assist_this = (
                    p1_low_assist_this
                    and self.P1_ASSIST_P2_ENABLED
                    and self.RECOVERY_ENABLED
                    and self.p2_recovery_deficit > 0
                )
                p1_this = p1_scheduled_this or p1_assist_this
                p2_this = p2_scheduled_this or p2_assist_this
                p1_recovery_leads_this = (
                    p1_this
                    and self.RECOVERY_ENABLED
                    and self.RECOVERY_SCOPE in ("combined", "smart")
                    and not p1_low_assist_this
                    and (self.recovery_deficit > 0 or self.p2_recovery_deficit > 0)
                )
                p2_recovery_suppressed_this = p2_this and p1_recovery_leads_this
                p1_was_assisting = (
                    p1_this
                    and p1_low_assist_this
                    and self.P1_ASSIST_P2_ENABLED
                    and self.RECOVERY_ENABLED
                    and self.p2_recovery_deficit > 0
                )
                p2_was_assisting = p2_assist_this
                p1_cashout_this = self.P1_ASSIST_CASHOUT if p1_was_assisting else self.PANEL1_CASHOUT

                # ── Set bet amounts for active panels ─────────────────────────
                try:
                    if p1_this:
                        if p1_was_assisting:
                            self.p1_bet = self._p1_assist_p2_bet()
                            await self._setup_one_panel(frame, 0, self.P1_ASSIST_CASHOUT, self.p1_bet)
                        else:
                            p1_extra_risk = self.P2_BET_AMOUNT if p2_recovery_suppressed_this else 0.0
                            self.p1_bet = self._p1_bet(extra_risk=p1_extra_risk)
                        if self.p1_bet != self.BET_AMOUNT:
                            await self._set_panel1_bet(frame, self.p1_bet)
                    if p2_this:
                        next_p2_bet = self.P2_BET_AMOUNT if p2_recovery_suppressed_this else self._p2_bet()
                        if self.p2_bet != next_p2_bet:
                            await self._set_panel2_bet(frame, next_p2_bet)
                        self.p2_bet = next_p2_bet
                    if p1_this or p2_this:
                        self._set_phase("betting", (
                            f"Placing bets — P1={'%.2f KES' % self.p1_bet if p1_this else 'skip'}"
                            f" P2={'%.2f KES' % self.p2_bet if p2_this else 'skip'}"
                        ))
                except Exception as e:
                    self.log.warning("Frame stale setting bets (%s) — skipping round.", e)
                    if self.DEMO_MODE:
                        try:
                            frame = await self._reconnect_demo()
                        except Exception as re:
                            self.log.error("Reconnect failed: %s — aborting.", re)
                            break
                    continue

                prev_history = await get_crash_history(frame)

                # ── Place bets for active panels ──────────────────────────────
                if p1_this or p2_this:
                    try:
                        placed = await self.place_bets(frame, p1=p1_this, p2=p2_this)
                    except Exception as e:
                        self.log.warning("Frame stale placing bet (%s) — skipping round.", e)
                        if self.DEMO_MODE:
                            try:
                                frame = await self._reconnect_demo()
                            except Exception as re:
                                self.log.error("Reconnect failed: %s — aborting.", re)
                                break
                        continue
                    if not placed:
                        self.log.warning("Could not place bets — skipping round.")
                        continue

                # ── Wait for round end ────────────────────────────────────────
                try:
                    history = await wait_for_round_end(frame, prev_history)
                except TimeoutError:
                    if self.DEMO_MODE:
                        self.log.warning("Round end timeout — reconnecting demo and continuing.")
                        try:
                            frame = await self._reconnect_demo()
                            continue
                        except Exception as e:
                            self.log.error("Reconnect failed: %s — aborting.", e)
                            break
                    self.log.error("Round end timeout — resetting both panels to watch.")
                    p1_bet_plan = []
                    p1_assist_plan = []
                    p2_bet_plan = []
                    continue
                except Exception as e:
                    if self.DEMO_MODE:
                        self.log.warning("Frame stale waiting for round end (%s) — reconnecting demo.", e)
                        try:
                            frame = await self._reconnect_demo()
                            continue
                        except Exception as re:
                            self.log.error("Reconnect failed: %s — aborting.", re)
                            break
                    self.log.warning("Frame stale waiting for round end (%s) — resetting.", e)
                    p1_bet_plan = []
                    p1_assist_plan = []
                    p2_bet_plan = []
                    continue

                crash_mult = history[0]

                # ── Process results for betting panels ────────────────────────
                if p1_this or p2_this:
                    p1_bet_used = self.p1_bet if p1_this else 0.0
                    p2_bet_used = self.p2_bet if p2_this else 0.0
                    round_pnl, desc = self._round_pnl(
                        crash_mult,
                        p1_bet_used,
                        p2_bet_used,
                        p1_cashout=p1_cashout_this,
                    )
                    self.cumulative_pnl += round_pnl
                    self.total_rounds   += 1
                    if round_pnl > 0:
                        self.total_wins += 1
                    else:
                        self.total_losses += 1
                    self._set_phase("round_complete", (
                        f"Round {self.total_rounds}: crash={crash_mult:.2f}x "
                        f"round={round_pnl:+.2f} total={self.cumulative_pnl:.2f} KES"
                    ))
                    self.log.info("ROUND %d | %s | round=%.2f KES | total=%.2f KES",
                                  self.total_rounds, desc, round_pnl, self.cumulative_pnl)
                    await self._read_balance()
                    self.csv.record(
                        crash_mult,
                        total_win=self.cumulative_pnl,
                        running_balance_after_bet=self._running_balance_text(),
                    )
                    self.log.info("RUNNING BALANCE AFTER BET: %s", self._running_balance_text())
                    self._log_status_snapshot(f"ROUND {self.total_rounds}")

                    # ── P1 result ─────────────────────────────────────────────
                    if p1_this:
                        p1_session_pnl += p1_bet_used * (p1_cashout_this - 1) if crash_mult >= p1_cashout_this else -p1_bet_used
                        if crash_mult >= p1_cashout_this:
                            if p1_was_assisting:
                                p1_net_gain = round(p1_bet_used * (p1_cashout_this - 1), 2)
                                old_p2_def = self.p2_recovery_deficit
                                self.p2_recovery_deficit = max(0.0, round(self.p2_recovery_deficit - p1_net_gain, 2))
                                self.log.info("P1 ASSIST WIN %.2fx @ %.1fx — P2 deficit %.2f → %.2f KES.",
                                              crash_mult, p1_cashout_this, old_p2_def, self.p2_recovery_deficit)
                                if self.p2_recovery_deficit <= 0:
                                    self._p2_step = 0
                            elif self.RECOVERY_SCOPE == "percentage":
                                total = self.recovery_deficit + self.p2_recovery_deficit
                                max_steps = self.RECOVERY_STEPS if self.RECOVERY_STEPS > 0 else self.P1_MAX_BET_ROUNDS
                                was_last  = (self._p1_step + 1) >= max_steps
                                target = total if was_last else total * self.RECOVERY_PERCENTAGE / 100
                                new_combined = round(max(0.0, total - target), 2)
                                self.log.info("P1 WIN %.2fx — %s → %.2f KES deficit remaining.",
                                              crash_mult,
                                              "full recovery" if was_last else f"{self.RECOVERY_PERCENTAGE}% recovery",
                                              new_combined)
                                self.recovery_deficit    = new_combined
                                self.p2_recovery_deficit = 0.0
                            else:
                                self.log.info("P1 WIN %.2fx — deficit cleared (was %.2f KES).",
                                              crash_mult, self.recovery_deficit)
                                self.recovery_deficit = 0.0
                                if self.RECOVERY_SCOPE in ("combined", "smart"):
                                    self.p2_recovery_deficit = 0.0
                            self._p1_consecutive_losses = 0
                            p1_bet_plan    = []
                            p1_assist_plan = []
                            p1_session_pnl = 0.0
                            self._p1_cooldown = self.BURST_COOLDOWN
                            try:
                                if p1_was_assisting:
                                    await self._setup_one_panel(frame, 0, self.PANEL1_CASHOUT, self.BET_AMOUNT)
                                if self.p1_bet != self.BET_AMOUNT:
                                    await self._set_panel1_bet(frame, self.BET_AMOUNT)
                                    self.p1_bet = self.BET_AMOUNT
                            except Exception:
                                pass
                        else:
                            if p1_was_assisting:
                                self.recovery_deficit = round(self.recovery_deficit + p1_bet_used, 2)
                                self.log.info("P1 ASSIST LOSS %.2fx — P1 takes %.2f KES debt → P1 deficit %.2f KES.",
                                              crash_mult, p1_bet_used, self.recovery_deficit)
                            elif self.RECOVERY_ENABLED:
                                self.recovery_deficit = round(self.recovery_deficit + self.p1_bet, 2)
                                self.log.info("P1 LOSS — deficit %.2f KES → next bet %.2f KES.",
                                              self.recovery_deficit, self._p1_bet())
                            self._p1_consecutive_losses += 1
                            if (self.STOP_ON_CONSECUTIVE_LOSSES > 0
                                    and self._p1_consecutive_losses >= self.STOP_ON_CONSECUTIVE_LOSSES):
                                self.last_event = f"Stopped: {self._p1_consecutive_losses} consecutive P1 losses"
                                self.browser_phase = "stopping"
                                self.log.warning("P1 consecutive loss limit (%d) — stopping.", self._p1_consecutive_losses)
                                break
                            if not p1_bet_plan:
                                self.log.info("P1: pattern complete — back to WATCH. Deficit %.2f KES.",
                                              self.recovery_deficit)
                                p1_bet_plan    = []
                                p1_assist_plan = []
                                p1_session_pnl = 0.0
                                self._p1_cooldown = self.BURST_COOLDOWN + (
                                    self.TRIGGER_LOSS_COOLDOWN if not p1_was_assisting else 0
                                )
                                try:
                                    if p1_was_assisting:
                                        await self._setup_one_panel(frame, 0, self.PANEL1_CASHOUT, self.BET_AMOUNT)
                                    if self.p1_bet != self.BET_AMOUNT:
                                        await self._set_panel1_bet(frame, self.BET_AMOUNT)
                                        self.p1_bet = self.BET_AMOUNT
                                except Exception:
                                    pass

                        if self.RECOVERY_SCOPE == "percentage" and self.RECOVERY_ENABLED:
                            total_def = self.recovery_deficit + self.p2_recovery_deficit
                            if total_def <= 0:
                                self._p1_step = 0
                            else:
                                max_s = self.RECOVERY_STEPS if self.RECOVERY_STEPS > 0 else self.P1_MAX_BET_ROUNDS
                                self._p1_step = 0 if (self._p1_step + 1) >= max_s else self._p1_step + 1

                    # ── P2 result ─────────────────────────────────────────────
                    if p2_this:
                        p2_session_pnl += p2_bet_used * (self.PANEL2_CASHOUT - 1) if crash_mult >= self.PANEL2_CASHOUT else -p2_bet_used
                        if crash_mult >= self.PANEL2_CASHOUT:
                            if p2_recovery_suppressed_this:
                                self.log.info("P2 NORMAL WIN %.2fx — P1 recovery had priority; P2 deficit remains %.2f KES.",
                                              crash_mult, self.p2_recovery_deficit)
                            elif p2_was_assisting:
                                p2_net_gain = round(p2_bet_used * (self.PANEL2_CASHOUT - 1), 2)
                                old_p1_def = self.recovery_deficit
                                self.recovery_deficit = max(0.0, round(self.recovery_deficit - p2_net_gain, 2))
                                self.log.info("P2 ASSIST WIN %.2fx — P1 deficit %.2f → %.2f KES.",
                                              crash_mult, old_p1_def, self.recovery_deficit)
                            elif self.P2_RECOVERY_SCOPE == "percentage":
                                max_steps = self.P2_RECOVERY_STEPS if self.P2_RECOVERY_STEPS > 0 else self.P2_MAX_BET_ROUNDS
                                was_last  = (self._p2_step + 1) >= max_steps
                                target = self.p2_recovery_deficit if was_last else self.p2_recovery_deficit * self.P2_RECOVERY_PERCENTAGE / 100
                                remaining = round(max(0.0, self.p2_recovery_deficit - target), 2)
                                self.log.info("P2 WIN %.2fx — %s → %.2f KES P2 deficit remaining.",
                                              crash_mult,
                                              "full recovery" if was_last else f"{self.P2_RECOVERY_PERCENTAGE}% recovery",
                                              remaining)
                                self.p2_recovery_deficit = remaining
                            else:
                                if self.P2_RECOVERY_SCOPE == "combined":
                                    old_p1_def = self.recovery_deficit
                                    old_p2_def = self.p2_recovery_deficit
                                    self.recovery_deficit = 0.0
                                    self.log.info("P2 WIN %.2fx — combined deficit cleared (P1 %.2f, P2 %.2f).",
                                                  crash_mult, old_p1_def, old_p2_def)
                                else:
                                    self.log.info("P2 WIN %.2fx — P2 deficit cleared (P1 deficit %.2f KES unchanged).",
                                                  crash_mult, self.recovery_deficit)
                                self.p2_recovery_deficit = 0.0
                            self._p2_consecutive_losses = 0
                            p2_bet_plan    = []
                            p2_session_pnl = 0.0
                            self._p2_cooldown = self.BURST_COOLDOWN
                            try:
                                if self.p2_bet != self.P2_BET_AMOUNT:
                                    await self._set_panel2_bet(frame, self.P2_BET_AMOUNT)
                                    self.p2_bet = self.P2_BET_AMOUNT
                            except Exception:
                                pass
                        else:
                            if p2_recovery_suppressed_this:
                                self.log.info("P2 NORMAL LOSS %.2fx — P1 recovery had priority; P2 deficit remains %.2f KES.",
                                              crash_mult, self.p2_recovery_deficit)
                            elif p2_was_assisting:
                                self.p2_recovery_deficit = round(self.p2_recovery_deficit + p2_bet_used, 2)
                                self.log.info("P2 ASSIST LOSS %.2fx — P2 takes %.2f KES debt → P2 deficit %.2f KES.",
                                              crash_mult, p2_bet_used, self.p2_recovery_deficit)
                            elif self.P2_RECOVERY_ENABLED:
                                self.p2_recovery_deficit = round(self.p2_recovery_deficit + self.p2_bet, 2)
                                self.log.info("P2 LOSS — deficit %.2f KES → next bet %.2f KES.",
                                              self.p2_recovery_deficit, self._p2_bet())
                            self._p2_consecutive_losses += 1
                            if (self.STOP_ON_CONSECUTIVE_LOSSES > 0
                                    and self._p2_consecutive_losses >= self.STOP_ON_CONSECUTIVE_LOSSES):
                                self.last_event = f"Stopped: {self._p2_consecutive_losses} consecutive P2 losses"
                                self.browser_phase = "stopping"
                                self.log.warning("P2 consecutive loss limit (%d) — stopping.", self._p2_consecutive_losses)
                                break
                            if not p2_bet_plan:
                                self.log.info("P2: pattern complete — back to WATCH. Deficit %.2f KES.",
                                              self.p2_recovery_deficit)
                                p2_bet_plan    = []
                                p2_session_pnl = 0.0
                                self._p2_cooldown = self.BURST_COOLDOWN
                                try:
                                    if self.p2_bet != self.P2_BET_AMOUNT:
                                        await self._set_panel2_bet(frame, self.P2_BET_AMOUNT)
                                        self.p2_bet = self.P2_BET_AMOUNT
                                except Exception:
                                    pass

                        if self.P2_RECOVERY_SCOPE == "percentage" and self.P2_RECOVERY_ENABLED:
                            total_def = self.recovery_deficit + self.p2_recovery_deficit
                            if total_def <= 0:
                                self._p2_step = 0
                            else:
                                max_s = self.P2_RECOVERY_STEPS if self.P2_RECOVERY_STEPS > 0 else self.P2_MAX_BET_ROUNDS
                                self._p2_step = 0 if (self._p2_step + 1) >= max_s else self._p2_step + 1

                    if p1_recovery_leads_this and crash_mult >= self.PANEL1_CASHOUT:
                        old_p1_def = self.recovery_deficit
                        old_p2_def = self.p2_recovery_deficit
                        self.recovery_deficit = 0.0
                        self.p2_recovery_deficit = 0.0
                        self._p1_step = 0
                        self._p2_step = 0
                        self.log.info(
                            "P1 PRIORITY RECOVERY WIN %.2fx — all deficits cleared (P1 %.2f, P2 %.2f).",
                            crash_mult, old_p1_def, old_p2_def,
                        )

                else:
                    self.csv.record(
                        crash_mult,
                        total_win=self.cumulative_pnl,
                        running_balance_after_bet=self._running_balance_text(),
                    )
                    self._set_phase("watching", f"Watching — last crash {crash_mult:.2f}x | total={self.cumulative_pnl:.2f} KES")
                    self._log_status_snapshot(f"WATCH crash={crash_mult:.2f}x")

                # ── Check triggers for each panel independently ───────────────
                if not p1_bet_plan:
                    if self._p1_cooldown > 0:
                        self._p1_cooldown -= 1
                        self.log.info("P1 cooldown: %d round(s) left.", self._p1_cooldown)
                    else:
                        p1_trig_high = self.P1_TRIGGER_MULT < crash_mult <= self.P1_TRIGGER_MULT_MAX
                        p1_trig_assist = (
                            self.P1_ASSIST_P2_ENABLED
                            and self.p2_recovery_deficit > 0
                            and crash_mult <= self.P1_ASSIST_TRIGGER_MAX
                        )
                        recent = history[:self.P1_LOW_STREAK_COUNT]
                        p1_trig_low = (len(recent) >= self.P1_LOW_STREAK_COUNT
                                       and all(m <= self.P1_LOW_STREAK_MAX for m in recent))
                        self.log.info("P1 WATCH | crash=%.2fx | high=%s | low=%s | assist=%s",
                                      crash_mult, p1_trig_high, p1_trig_low, p1_trig_assist)
                        _combined_def = self.recovery_deficit + self.p2_recovery_deficit
                        _cap_active = self.RECOVERY_DEFICIT_CAP > 0 and _combined_def >= self.RECOVERY_DEFICIT_CAP
                        if p1_trig_assist:
                            p1_reason = (
                                f"P2 assist: crash {crash_mult:.2f}x <= {self.P1_ASSIST_TRIGGER_MAX:.1f}x "
                                f"and P2 deficit {self.p2_recovery_deficit:.2f} KES"
                            )
                        elif p1_trig_high and _cap_active:
                            self.log.warning(
                                "P1 HIGH blocked — deficit KES %.2f >= cap KES %.2f; waiting to recover first.",
                                _combined_def, self.RECOVERY_DEFICIT_CAP,
                            )
                            p1_reason = None
                        elif p1_trig_high:
                            p1_reason = f"crash {crash_mult:.2f}x in ({self.P1_TRIGGER_MULT:.1f}x, {self.P1_TRIGGER_MULT_MAX:.1f}x]"
                        elif p1_trig_low:
                            p1_reason = f"last {self.P1_LOW_STREAK_COUNT} crashes all ≤ {self.P1_LOW_STREAK_MAX:.1f}x"
                        else:
                            p1_reason = None
                        if p1_reason:
                            self._set_phase("triggered", f"P1 TRIGGER: {p1_reason} | {format_bet_pattern(self.P1_BET_PATTERN)}")
                            self.log.info("P1 TRIGGER (%s) — pattern %s", p1_reason, format_bet_pattern(self.P1_BET_PATTERN))
                            p1_bet_plan    = list(self.P1_BET_PATTERN)
                            p1_assist_plan = [p1_trig_assist and bool(step) for step in p1_bet_plan]
                            p1_session_pnl = 0.0

                if not p2_bet_plan:
                    if self._p2_cooldown > 0:
                        self._p2_cooldown -= 1
                        self.log.info("P2 cooldown: %d round(s) left.", self._p2_cooldown)
                    else:
                        p2_trig_high = False
                        recent = history[:self.P2_LOW_STREAK_COUNT]
                        p2_trig_low = (len(recent) >= self.P2_LOW_STREAK_COUNT
                                       and all(self.P2_LOW_STREAK_MIN < m < self.P2_LOW_STREAK_MAX for m in recent))
                        self.log.info("P2 WATCH | crash=%.2fx | high=%s | low=%s", crash_mult, p2_trig_high, p2_trig_low)
                        if p2_trig_high:
                            p2_reason = f"crash {crash_mult:.2f}x in [{self.P2_TRIGGER_MULT:.1f}x, {self.P2_TRIGGER_MULT_MAX:.1f}x]"
                        elif p2_trig_low:
                            p2_reason = (
                                f"last {self.P2_LOW_STREAK_COUNT} crashes all "
                                f"in ({self.P2_LOW_STREAK_MIN:.1f}x, {self.P2_LOW_STREAK_MAX:.1f}x)"
                            )
                        else:
                            p2_reason = None
                        if p2_reason:
                            self._set_phase("triggered", f"P2 TRIGGER: {p2_reason} | {format_bet_pattern(self.P2_BET_PATTERN)}")
                            self.log.info("P2 TRIGGER (%s) — pattern %s", p2_reason, format_bet_pattern(self.P2_BET_PATTERN))
                            p2_bet_plan    = list(self.P2_BET_PATTERN)
                            p2_session_pnl = 0.0

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
                self._set_phase("stopped", "Bot stopped (still logged in)")
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
        self._log_status_snapshot("FINAL")
        self.log.info("=" * 60)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    await AviatorBot().run()


if __name__ == "__main__":
    asyncio.run(main())
