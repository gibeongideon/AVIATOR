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
    # Green BET button — both panels use this class/text, but embedded builds vary a little
    "bet_btn":      (
        'button.btn-success.bet, button.bet, .buttons-block button.btn-success, '
        'app-bet-control .buttons-block button, button:has-text("BET"), button:has-text("Bet")'
    ),
    # Crash history bar (newest crash is first line)
    "history":      (
        'div.result-history, .result-history, .result-history-item, '
        '.payouts-block, .payouts-block .bubble-multiplier, .payout, '
        'app-stats-widget, app-stats-widget .bubble-multiplier, '
        '[class*="history"], [class*="payout"]'
    ),
}


# ── Low-level helpers ─────────────────────────────────────────────────────────

async def set_input(inp, value):
    """
    Set a value in an Angular reactive-form input.
    Angular does NOT always react to plain DOM value changes, so try real
    keyboard events first and fall back to native setter events if needed.
    """
    text = str(value)
    await inp.click()
    await asyncio.sleep(0.05)
    await inp.press("Control+a")
    await asyncio.sleep(0.05)
    await inp.type(text, delay=60)         # real keystrokes → Angular model updates
    await inp.press("Tab")                 # blur → triggers validators
    await asyncio.sleep(0.15)

    try:
        current = (await inp.input_value()).strip()
    except Exception:
        current = ""
    if current == text:
        return

    # Some embedded real-money builds ignore keyboard replacement unless the
    # native setter and Angular-style events are fired too.
    await inp.evaluate(
        """(el, val) => {
            const setter = Object.getOwnPropertyDescriptor(
                HTMLInputElement.prototype, 'value'
            ).set;
            setter.call(el, val);
            for (const name of ['input', 'change', 'blur']) {
                el.dispatchEvent(new Event(name, { bubbles: true }));
            }
        }""",
        text,
    )
    await asyncio.sleep(0.15)


async def get_crash_history(frame) -> list[float]:
    """
    Read crash history from .result-history.
    Returns list of multipliers, newest first.
    """
    try:
        texts = await frame.evaluate(
            """() => {
                const selectors = [
                    '.result-history .payout',
                    '.payouts-block .payout',
                    'app-stats-widget .payout',
                    '[appcoloredmultiplier]',
                    '.bubble-multiplier',
                    '.result-history',
                    '[class*="history"]',
                    '[class*="payout"]'
                ];
                for (const sel of selectors) {
                    const nodes = Array.from(document.querySelectorAll(sel));
                    const values = nodes
                        .map(el => (el.innerText || el.textContent || '').trim())
                        .filter(Boolean);
                    if (values.some(v => /\\d+(?:[.,]\\d+)?\\s*x/i.test(v))) {
                        return values;
                    }
                }
                return [];
            }"""
        )
        result = []
        for raw in texts:
            for token in re.findall(r"\d+(?:[.,]\d+)?\s*x", raw, flags=re.I):
                value = token.lower().replace("x", "").replace(",", ".").strip()
                try:
                    result.append(float(value))
                except ValueError:
                    pass
            if result:
                return result
        return []
    except Exception:
        return []


async def frame_probe(frame) -> dict:
    """Small diagnostic snapshot when real mode cannot see BET/history."""
    try:
        return await frame.evaluate(
            """() => ({
                url: location.href,
                title: document.title,
                readyState: document.readyState,
                payouts: Array.from(document.querySelectorAll(
                    '.payout, [appcoloredmultiplier], .bubble-multiplier, [class*="payout"]'
                )).slice(0, 12).map(el => (el.innerText || el.textContent || '').trim()).filter(Boolean),
                buttons: Array.from(document.querySelectorAll('button')).slice(0, 20).map(el => ({
                    text: (el.innerText || el.textContent || '').trim(),
                    cls: el.className || '',
                    disabled: !!el.disabled
                })),
                inputs: Array.from(document.querySelectorAll('input')).slice(0, 12).map(el => ({
                    value: el.value || '',
                    placeholder: el.getAttribute('placeholder') || '',
                    disabled: !!el.disabled
                }))
            })}"""
        )
    except Exception as e:
        return {"error": str(e), "url": getattr(frame, "url", "")}


async def wait_for_bet_phase(
    frame,
    timeout_s: int = 120,
    prev_history: list[float] | None = None,
) -> tuple[str, list[float] | None]:
    """Wait until betting opens, or a new crash proves the window was missed."""
    for _ in range(timeout_s * 4):
        btns = await get_bet_buttons(frame)
        if btns:
            return "bet", None
        if prev_history is not None:
            hist = await get_crash_history(frame)
            if hist and (not prev_history or hist[0] != prev_history[0]):
                return "history", hist
        await asyncio.sleep(0.25)
    return "timeout", None


async def wait_for_bet_phase_or_history_change(
    frame,
    prev_history: list[float],
    timeout_s: int = 120,
) -> tuple[str, list[float] | None]:
    """
    Wait for either an open betting window or a new crash result.

    This keeps real-money embeds moving even when their BET button is only
    visible briefly or uses a slightly different rendering from demo mode.
    """
    for _ in range(timeout_s * 4):
        btns = await get_bet_buttons(frame)
        if btns:
            return "bet", None
        hist = await get_crash_history(frame)
        if hist and (not prev_history or hist[0] != prev_history[0]):
            return "history", hist
        await asyncio.sleep(0.25)
    return "timeout", None


async def get_bet_buttons(frame):
    """Return visible, enabled buttons that look like active BET buttons."""
    result = []
    for btn in await frame.query_selector_all(SEL["bet_btn"]):
        try:
            if not await btn.is_visible() or not await btn.is_enabled():
                continue
            text = ((await btn.inner_text()) or "").upper()
            cls = (await btn.get_attribute("class") or "").lower()
            compact_text = " ".join(text.split())
            if "CANCEL" in text or "CASH" in text:
                continue
            if "tab" in cls or compact_text in {"ALL BETS", "MY BETS", "TOP"}:
                continue
            if "disabled" in cls:
                continue
            aria = ((await btn.get_attribute("aria-label")) or "").upper()
            data_testid = ((await btn.get_attribute("data-testid")) or "").lower()
            in_bet_control = await btn.evaluate(
                """el => !!el.closest('app-bet-control .buttons-block')"""
            )
            receives_pointer = await btn.evaluate(
                """el => {
                    const rect = el.getBoundingClientRect();
                    if (!rect.width || !rect.height) return false;
                    const x = rect.left + rect.width / 2;
                    const y = rect.top + rect.height / 2;
                    const top = document.elementFromPoint(x, y);
                    return !!top && (el === top || el.contains(top));
                }"""
            )
            if not receives_pointer:
                continue
            if (
                compact_text == "BET"
                or compact_text.startswith("BET ")
                or "BET" in aria
                or "btn-success" in cls
                or "bet" in data_testid
                or ("bet" in cls and "btn" in cls)
                or in_bet_control
            ):
                result.append(btn)
        except Exception:
            continue
    return result


async def bet_button_state(btn) -> str:
    try:
        text = " ".join(((await btn.inner_text()) or "").upper().split())
        cls = (await btn.get_attribute("class") or "").lower()
        return f"text={text!r} class={cls!r}"
    except Exception as e:
        return f"<stale: {e}>"


async def button_still_accepts_bet(btn) -> bool:
    try:
        if not await btn.is_visible() or not await btn.is_enabled():
            return False
        text = ((await btn.inner_text()) or "").upper()
        cls = (await btn.get_attribute("class") or "").lower()
        if "CANCEL" in text or "CASH" in text or "disabled" in cls:
            return False
        return True
    except Exception:
        return False


async def click_bet_button(btn) -> bool:
    """Click a bet button and verify that the game accepted it."""
    for attempt in range(3):
        try:
            await btn.scroll_into_view_if_needed(timeout=1000)
            if attempt == 0:
                await btn.click(timeout=1200)
            elif attempt == 1:
                await btn.click(timeout=1200, force=True)
            else:
                await btn.evaluate(
                    """el => {
                        el.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
                        el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                        el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                        el.dispatchEvent(new PointerEvent('pointerup', {bubbles: true}));
                        el.click();
                    }"""
                )
            await asyncio.sleep(0.25)
            if not await button_still_accepts_bet(btn):
                return True
        except Exception:
            await asyncio.sleep(0.15)
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
        self.STOP_ON_DRAWDOWN_PCT    = s.get("stop_on_drawdown_pct",   getattr(config, "STOP_ON_DRAWDOWN_PCT", 0))
        self.DRAWDOWN_PROTECTION_PCT = s.get("drawdown_protection_pct", getattr(config, "DRAWDOWN_PROTECTION_PCT", 0))
        self.STOP_PROFIT_LOSS_FRAC     = s.get("stop_profit_loss_frac",     getattr(config, "STOP_PROFIT_LOSS_FRAC", 0))
        self.STOP_PROFIT_LOSS_FRAC_MAX = s.get("stop_profit_loss_frac_max", getattr(config, "STOP_PROFIT_LOSS_FRAC_MAX", self.STOP_PROFIT_LOSS_FRAC))
        self.AUTO_RESTART_SESSION    = s.get("auto_restart_session",   getattr(config, "AUTO_RESTART_SESSION", False))
        self.RESTART_DELAY           = s.get("restart_delay",          getattr(config, "RESTART_DELAY", 10))
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
        self.RECOVERY_CHUNK_CAP        = s.get("recovery_chunk_cap",         getattr(config, "RECOVERY_CHUNK_CAP", 0))
        self.RECOVERY_CHUNK_CAP_PCT    = s.get("recovery_chunk_cap_pct",     getattr(config, "RECOVERY_CHUNK_CAP_PCT", 0))
        self.INITIAL_BALANCE           = s.get("initial_balance",            getattr(config, "INITIAL_BALANCE", 0))
        self.INITIAL_DEMO_BALANCE      = s.get("initial_demo_balance",       getattr(config, "INITIAL_DEMO_BALANCE", 0))
        self.MIN_TRIGGER_CRASH         = s.get("min_trigger_crash",          getattr(config, "MIN_TRIGGER_CRASH", 0.0))
        self.P1_FOLLOW_P2              = s.get("p1_follow_p2",               getattr(config, "P1_FOLLOW_P2", False))
        self.P2_FOLLOW_P1              = s.get("p2_follow_p1",               getattr(config, "P2_FOLLOW_P1", False))
        self.P1_LOW_ZONE_ENABLED       = s.get("p1_low_zone_enabled",        getattr(config, "P1_LOW_ZONE_ENABLED", False))
        self.P1_LOW_ZONE_MAX           = s.get("p1_low_zone_max",            getattr(config, "P1_LOW_ZONE_MAX", 1.4))
        self.P1_LOW_ZONE_CASHOUT       = s.get("p1_low_zone_cashout",        getattr(config, "P1_LOW_ZONE_CASHOUT", 1.5))
        self.P1_LOW_ZONE_PERCENTAGE    = s.get("p1_low_zone_percentage",     getattr(config, "P1_LOW_ZONE_PERCENTAGE", 50))
        self.AM_STRATEGY_ENABLED       = s.get("am_strategy_enabled",       getattr(config, "AM_STRATEGY_ENABLED", False))
        self.AM_TRIGGER_CRASH          = s.get("am_trigger_crash",          getattr(config, "AM_TRIGGER_CRASH", 8.0))
        self.AM_CASHOUT                = s.get("am_cashout",                getattr(config, "AM_CASHOUT", 7.0))
        self.AM_BET_AMOUNT             = s.get("am_bet_amount",             getattr(config, "AM_BET_AMOUNT", 50.0))
        self.AM_MAX_STREAK             = s.get("am_max_streak",             getattr(config, "AM_MAX_STREAK", 4))
        self.AM_MAX_BET                = s.get("am_max_bet",                getattr(config, "AM_MAX_BET", 5000.0))
        self.P2_AM_ENABLED             = s.get("p2_am_enabled",             getattr(config, "P2_AM_ENABLED", False))
        self.P2_AM_TRIGGER_CRASH       = s.get("p2_am_trigger_crash",       getattr(config, "P2_AM_TRIGGER_CRASH", 8.0))
        self.P2_AM_CASHOUT             = s.get("p2_am_cashout",             getattr(config, "P2_AM_CASHOUT", 8.0))
        self.P2_AM_BET_AMOUNT          = s.get("p2_am_bet_amount",          getattr(config, "P2_AM_BET_AMOUNT", 50.0))
        self.P2_AM_MAX_STREAK          = s.get("p2_am_max_streak",          getattr(config, "P2_AM_MAX_STREAK", 4))
        self.P2_AM_MAX_BET             = s.get("p2_am_max_bet",             getattr(config, "P2_AM_MAX_BET", 5000.0))
        self.PREDICTOR_ENABLED         = s.get("predictor_enabled",         getattr(config, "PREDICTOR_ENABLED", False))
        self.PREDICTOR_RETRAIN_ROUNDS  = s.get("predictor_retrain_rounds",  getattr(config, "PREDICTOR_RETRAIN_ROUNDS", 500))
        self.PREDICTOR_MIN_ROUNDS      = s.get("predictor_min_rounds",      getattr(config, "PREDICTOR_MIN_ROUNDS", 1000))
        self.PREDICTOR_P1_CONFIDENCE   = s.get("predictor_p1_confidence",   getattr(config, "PREDICTOR_P1_CONFIDENCE", 1.0))
        self.PREDICTOR_P2_CONFIDENCE   = s.get("predictor_p2_confidence",   getattr(config, "PREDICTOR_P2_CONFIDENCE", 1.0))

        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page:    Optional[Page]    = None

        self.total_rounds    = 0
        self.total_wins      = 0
        self.total_losses    = 0
        self.session_count   = 0
        self.lifetime_pnl    = 0.0   # cumulative PnL across all auto-restart sessions
        self.cumulative_pnl  = 0.0
        self.pending_bet     = 0.0  # KES currently at risk (deducted on place, cleared on settle)
        self.peak_pnl        = 0.0   # highest PnL reached this session (for drawdown stop)

        self.session_start_time: Optional[datetime] = None
        self._last_stop_reason  = "Bot exited"

        initial_demo_balance = getattr(config, "INITIAL_DEMO_BALANCE", None)
        self.recovery_deficit    = 0.0
        self.p2_recovery_deficit = 0.0
        self.drawdown_protection_active = False
        self._drawdown_threshold_kes    = 0.0
        self._am_bet    = 0.0
        self._am_streak = 0
        self._p2_am_bet    = 0.0
        self._p2_am_streak = 0
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
        self.pending_bet         = 0.0
        self._demo_reconnects    = 0   # how many times we have reopened the demo tab

        self.csv = HistoryCSV(
            session_id=self._session_id,
            panel1_cashout=self.PANEL1_CASHOUT,
            panel2_cashout=self.PANEL2_CASHOUT,
            trigger_mult=self.P1_TRIGGER_MULT,
        )

        # ── ML predictor (optional confidence gate) ───────────────────────────
        self._predictor = None
        if self.PREDICTOR_ENABLED:
            try:
                from predictor import AutoRetrainPredictor, load_crashes
                _hist = load_crashes().tolist()
                self._predictor = AutoRetrainPredictor(
                    retrain_every=self.PREDICTOR_RETRAIN_ROUNDS,
                    min_train_rounds=self.PREDICTOR_MIN_ROUNDS,
                    initial_data=_hist,
                )
                self.log.info("Predictor: initialised with %d historical rounds; first train queued.", len(_hist))
            except Exception as _pred_exc:
                self.log.warning("Predictor disabled — %s", _pred_exc)

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
        tag = " [BET LIVE]" if self.pending_bet > 0 else ""
        tracked_demo = self._tracked_demo_balance()
        if tracked_demo is not None:
            live = round(tracked_demo - self.pending_bet, 2)
            return f"{live:,.2f} KES{tag}"
        if self.account_balance and self.account_balance != "—":
            return self.account_balance
        pnl_net = self.cumulative_pnl - self.pending_bet
        return f"P&L {pnl_net:+.2f} KES{tag}"

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

    def _effective_chunk_cap(self) -> float:
        if self.RECOVERY_CHUNK_CAP_PCT > 0:
            bal = self.INITIAL_DEMO_BALANCE if self.DEMO_MODE and self.INITIAL_DEMO_BALANCE > 0 else self.INITIAL_BALANCE
            if bal > 0:
                return round(bal * self.RECOVERY_CHUNK_CAP_PCT / 100, 2)
        return self.RECOVERY_CHUNK_CAP

    def _p1_bet(self, extra_risk: float = 0.0) -> float:
        if not self.RECOVERY_ENABLED:
            return self.BET_AMOUNT
        p1d = self.recovery_deficit
        p2d = self.p2_recovery_deficit
        if self.drawdown_protection_active:
            _cap   = self._drawdown_threshold_kes
            _total = p1d + p2d
            if _total > _cap:
                _ratio = p1d / _total if _total > 0 else 1.0
                p1d = round(_cap * _ratio, 2)
                p2d = round(_cap * (1.0 - _ratio), 2)
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
        _cap = self._effective_chunk_cap()
        if _cap > 0 and target > _cap:
            target = _cap
        net_multiplier = max(0.01, self.PANEL1_CASHOUT - 1)
        return max(self.BET_AMOUNT,
                   round((target + extra_risk + self.RECOVERY_PROFIT_TARGET) / net_multiplier, 2))

    def _p2_bet(self, extra_risk: float = 0.0) -> float:
        if not self.P2_RECOVERY_ENABLED:
            return self.P2_BET_AMOUNT
        p1d = self.recovery_deficit
        p2d = self.p2_recovery_deficit
        if self.drawdown_protection_active:
            _cap   = self._drawdown_threshold_kes
            _total = p1d + p2d
            if _total > _cap:
                _ratio = p1d / _total if _total > 0 else 1.0
                p1d = round(_cap * _ratio, 2)
                p2d = round(_cap * (1.0 - _ratio), 2)
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
        chunk_cap = self._effective_chunk_cap()
        if chunk_cap > 0 and target > chunk_cap:
            target = chunk_cap
        net_multiplier = max(0.01, self.PANEL2_CASHOUT - 1)
        return max(self.P2_BET_AMOUNT,
                   round((target + extra_risk + self.P2_RECOVERY_PROFIT_TARGET) / net_multiplier, 2))

    def _p1_assist_p2_bet(self) -> float:
        if not self.P1_ASSIST_P2_ENABLED or self.p2_recovery_deficit <= 0:
            return self.BET_AMOUNT
        target = self.p2_recovery_deficit * self.P1_ASSIST_PERCENTAGE / 100
        chunk_cap = self._effective_chunk_cap()
        if chunk_cap > 0 and target > chunk_cap:
            target = chunk_cap
        net_multiplier = max(0.01, self.P1_ASSIST_CASHOUT - 1)
        return max(self.BET_AMOUNT, round((target + self.RECOVERY_PROFIT_TARGET) / net_multiplier, 2))

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
                event, observed_history = await wait_for_bet_phase(
                    frame,
                    timeout_s=15,
                    prev_history=history,
                )
                ok = event in ("bet", "history")
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
                probe = await frame_probe(frame)
                self.log.warning(
                    "AI: bet phase/history not detected — continuing to watch. "
                    "frame=%s payouts=%s buttons=%s inputs=%s",
                    str(probe.get("url", frame.url))[:90],
                    probe.get("payouts"),
                    probe.get("buttons"),
                    probe.get("inputs"),
                )
                continue

            if observed_history is not None:
                history = observed_history
                continue

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
        self._set_phase("launching", f"Launching {'headless' if self._headless else 'visible'} browser…")
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
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
        try:
            if self.context:
                await self.context.close()
        except Exception:
            pass
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass
        self.context = None
        self.browser = None
        self.playwright = None

    # ── Login ─────────────────────────────────────────────────────────────────

    async def _login_form_visible(self) -> bool:
        for sel in (SEL["login_user"], SEL["login_pass"]):
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _login_cta_visible(self) -> bool:
        for sel in (
            SEL["login_btn"],
            'button:has-text("Login")',
            'button:has-text("Log in")',
            'a:has-text("Login")',
            'a:has-text("Log in")',
        ):
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _has_login_success_marker(self) -> bool:
        for sel in (
            '[data-testid="logout"]',
            'a[href*="logout"]',
            'button:has-text("Logout")',
            'button:has-text("Sign out")',
            'a:has-text("Logout")',
            'button:has-text("Deposit")',
            'a:has-text("Deposit")',
            '[class*="balance"]',
            '[class*="wallet"]',
        ):
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    return True
            except Exception:
                continue
        url = (self.page.url or "").lower()
        return (
            "login" not in url
            and not await self._login_form_visible()
            and not await self._login_cta_visible()
        )

    async def _wait_for_login_success(self, timeout_s: int = 30) -> bool:
        for _ in range(timeout_s * 2):
            if await self._has_login_success_marker():
                return True
            await asyncio.sleep(0.5)

        # Some SportPesa sessions set auth cookies but leave the SPA on /login.
        # Probe the target page before deciding login really failed.
        try:
            await self.page.goto(config.AVIATOR_URL, wait_until="domcontentloaded", timeout=15_000)
            await self.page.wait_for_timeout(2000)
        except PWTimeout:
            pass
        return await self._has_login_success_marker()

    async def login(self):
        self._set_phase("logging_in", "Logging in…")
        self.log.info("Logging in…")
        await self.page.goto(config.LOGIN_URL, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(2000)
        await self.page.fill(SEL["login_user"], self._username)
        await self.page.fill(SEL["login_pass"], self._password)
        await self.page.click(SEL["login_btn"])
        if await self._wait_for_login_success(timeout_s=30):
            self._set_phase("logged_in", "Login successful")
            self.log.info("Login successful.")
            await self._dismiss_page_popups()
            await self._read_balance()
            return
        self._set_phase("error", "Login failed — check credentials")
        self.log.error("Login may have failed — url=%s login_form_visible=%s",
                       self.page.url, await self._login_form_visible())
        raise TimeoutError("Login did not reach an authenticated page")

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

    def _is_known_game_url(self, url: str) -> bool:
        url = (url or "").lower()
        return any(marker in url for marker in (
            "spribegaming.com",
            "spribe.co",
            "spribe.io",
            "aviator-next",
            "aviator-demo",
        ))

    async def _frame_has_game_markers(self, frame) -> bool:
        """Return True when a frame looks like the Aviator game UI."""
        if self._is_known_game_url(frame.url):
            return True
        for sel in (
            SEL["bet_btn"],
            SEL["history"],
            SEL["cashout_input_in_spinner"],
            ".cash-out-switcher",
            ".result-history",
            "app-root",
        ):
            try:
                if await frame.query_selector(sel):
                    return True
            except Exception:
                continue
        return False

    async def _get_bet_inputs(self, frame):
        """Return visible bet amount inputs, excluding auto cashout fields."""
        bet_inputs = []
        for inp in await frame.query_selector_all("input"):
            try:
                if not await inp.is_visible():
                    continue
                is_cashout = await inp.evaluate(
                    """el => !!el.closest(
                        '.cashout-spinner-wrapper, .cashout-spinner, ' +
                        '.cash-out-switcher, .cashout-block'
                    )"""
                )
                if is_cashout:
                    continue
                input_type = (await inp.get_attribute("type") or "").lower()
                if input_type in ("hidden", "checkbox", "radio"):
                    continue
                bet_inputs.append(inp)
            except Exception:
                continue
        return bet_inputs

    def _frame_debug_snapshot(self) -> str:
        rows = []
        for idx, frame in enumerate(self.page.frames if self.page else []):
            url = (frame.url or "about:blank")[:120]
            rows.append(f"{idx}:{url}")
        return " | ".join(rows) if rows else "<no frames>"

    def _get_frame(self):
        """
        Return the live Spribe game frame.
        Demo mode: self.page IS the spribegaming tab — main frame is the game.
        SportPesa mode: game lives inside an iframe in the casino-frontend.
        Never cache the result — the iframe reloads periodically.
        """
        # Demo mode: self.page IS the Spribe tab
        if self._is_known_game_url(self.page.url):
            return self.page.main_frame
        # SportPesa mode: game runs inside an iframe. Provider URLs change, so
        # keep this broad; _wait_for_frame verifies the frame has game controls.
        for f in reversed(self.page.frames):
            if self._is_known_game_url(f.url):
                return f
        return None

    async def _wait_for_frame(self, timeout_s=30):
        """Poll until the Spribe frame is present and has bet inputs loaded."""
        demo_attempted = False
        last_debug = -1
        for _ in range(timeout_s * 2):
            candidates = []
            known = self._get_frame()
            if known:
                candidates.append(known)
            if self.page:
                candidates.extend([f for f in self.page.frames if f not in candidates])

            for frame in candidates:
                try:
                    # If Demo mode is on and we haven't tried yet, do it now
                    if self.DEMO_MODE and not demo_attempted:
                        await self._select_demo_mode(frame)
                        demo_attempted = True
                    bet_inputs = await self._get_bet_inputs(frame)
                    if bet_inputs and await self._frame_has_game_markers(frame):
                        self.log.info("Aviator frame ready: %s (%d bet inputs)", frame.url[:90], len(bet_inputs))
                        return frame
                except Exception:
                    continue
            elapsed = (_ + 1) / 2
            if elapsed - last_debug >= 10:
                last_debug = elapsed
                self.log.info("Still waiting for Aviator inputs. Frames: %s", self._frame_debug_snapshot())
            await asyncio.sleep(0.5)
        raise TimeoutError(
            "Spribe game frame with inputs not ready after %ds. Frames: %s"
            % (timeout_s, self._frame_debug_snapshot())
        )

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
            await self.stop()
        except Exception:
            self.browser = None
            self.context = None
            self.page = None
            self.playwright = None

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
        bet_inputs = await self._get_bet_inputs(frame)
        if panel_idx < len(bet_inputs):
            await set_input(bet_inputs[panel_idx], _bet)
            self.log.info("  Panel %d: bet amount set to %s KES.", panel_idx, _bet)
        else:
            self.log.warning("  Panel %d: bet amount input not found (%d found).", panel_idx, len(bet_inputs))

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
        bet_inputs = await self._get_bet_inputs(frame)
        if bet_inputs:
            await set_input(bet_inputs[0], amount)
            self.log.info("P1 bet → %.2f KES (P1 deficit: %.2f KES).", amount, self.recovery_deficit)

    async def _set_panel2_bet(self, frame, amount: float):
        bet_inputs = await self._get_bet_inputs(frame)
        if len(bet_inputs) > 1:
            await set_input(bet_inputs[1], amount)
            self.log.info("P2 bet → %.2f KES (P2 deficit: %.2f KES).", amount, self.p2_recovery_deficit)

    # ── Place bets on both panels ─────────────────────────────────────────────

    async def place_bets(self, frame, p1: bool = True, p2: bool = True) -> bool:
        btns = await get_bet_buttons(frame)
        if not btns:
            self.log.warning("BET buttons not found — bet phase may have already closed.")
            return False
        placed = False
        if p1 and len(btns) > 0:
            before = await bet_button_state(btns[0])
            if await click_bet_button(btns[0]):
                placed = True
            else:
                after = await bet_button_state(btns[0])
                self.log.warning("P1 bet click was not accepted. before=%s after=%s", before, after)
        if p2 and len(btns) > 1:
            await asyncio.sleep(0.1)
            before = await bet_button_state(btns[1])
            if await click_bet_button(btns[1]):
                placed = True
            else:
                after = await bet_button_state(btns[1])
                self.log.warning("P2 bet click was not accepted. before=%s after=%s", before, after)
        self.log.info("Bets placed — P1=%s P2=%s accepted=%s.", p1, p2, placed)
        return placed

    # ── Global stop checks ────────────────────────────────────────────────────

    def should_stop(self) -> Optional[str]:
        if self.cumulative_pnl > self.peak_pnl:
            self.peak_pnl = self.cumulative_pnl
        reason = None
        if self.STOP_ON_LOSS < 0 and self.cumulative_pnl <= self.STOP_ON_LOSS:
            reason = f"Loss limit hit (KES {self.cumulative_pnl:.2f})"
        elif self.STOP_ON_DRAWDOWN_PCT > 0 and self.peak_pnl >= self.STOP_ON_PROFIT > 0:
            allowed_drawdown = self.peak_pnl * self.STOP_ON_DRAWDOWN_PCT / 100
            drawdown = self.peak_pnl - self.cumulative_pnl
            if drawdown >= allowed_drawdown:
                reason = (f"Drawdown limit hit — peak {self.peak_pnl:.2f} KES, "
                          f"now {self.cumulative_pnl:.2f} KES "
                          f"(dropped {drawdown:.2f} / {allowed_drawdown:.2f} KES allowed)")
        elif self.STOP_ON_PROFIT > 0 and self.cumulative_pnl >= self.STOP_ON_PROFIT:
            reason = f"Profit target reached (KES {self.cumulative_pnl:.2f})"
        if reason:
            self._last_stop_reason = reason
        return reason

    def _update_drawdown_protection(self) -> None:
        pct = getattr(self, "DRAWDOWN_PROTECTION_PCT", 0)
        if pct <= 0:
            return
        if self._drawdown_threshold_kes == 0.0:
            # Always use INITIAL_BALANCE (real bankroll) so threshold stays
            # below the chunk cap (which uses INITIAL_DEMO_BALANCE in demo mode)
            bal = getattr(self, "INITIAL_BALANCE", 0)
            self._drawdown_threshold_kes = round(bal * pct / 100, 2) if bal > 0 else 0.0
        threshold = self._drawdown_threshold_kes
        if threshold <= 0:
            return
        drawdown = self.peak_pnl - self.cumulative_pnl
        was_active = self.drawdown_protection_active
        if drawdown >= threshold:
            self.drawdown_protection_active = True
            if not was_active:
                self.log.warning(
                    "DRAWDOWN PROTECTION ON — peak %.2f KES, now %.2f KES "
                    "(dropped %.2f / %.2f KES allowed). Capping recovery target.",
                    self.peak_pnl, self.cumulative_pnl, drawdown, threshold,
                )
        elif self.drawdown_protection_active and drawdown < threshold * 0.5:
            self.drawdown_protection_active = False
            self.log.info("Drawdown protection OFF — recovered above 50%% of trigger threshold.")

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self):
        await self.start()
        try:
            while True:   # session loop — repeats if AUTO_RESTART_SESSION = True
                self.session_count += 1
                self.session_start_time = datetime.now()
                self._last_stop_reason  = "Bot exited"

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
                p1_bet_plan      = []
                p1_assist_plan   = []
                p1_follow_plan   = []
                p1_low_zone_plan = []
                p1_session_pnl = 0.0

                p2_bet_plan    = []
                p2_session_pnl = 0.0

                history = await get_crash_history(frame)
                self.log.info("Initial crash history sample: %s", history[:8])

                self._set_phase("watching", "Strategy active — watching for trigger")
                self.log.info("=" * 60)
                self.log.info("SESSION %d — Strategy active — INDEPENDENT TRIGGERS", self.session_count)
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
            if getattr(self, "AM_STRATEGY_ENABLED", False):
                self.log.info("  AM MODE ON — P1: trigger ≥ %.1fx | cashout %.1fx | base %.0f KES | streak %d | cap %.0f KES",
                              self.AM_TRIGGER_CRASH, self.AM_CASHOUT,
                              self.AM_BET_AMOUNT, self.AM_MAX_STREAK, self.AM_MAX_BET)
                if getattr(self, "P2_AM_ENABLED", False):
                    self.log.info("  AM MODE ON — P2: trigger ≥ %.1fx | cashout %.1fx | base %.0f KES | streak %d | cap %.0f KES",
                                  self.P2_AM_TRIGGER_CRASH, self.P2_AM_CASHOUT,
                                  self.P2_AM_BET_AMOUNT, self.P2_AM_MAX_STREAK, self.P2_AM_MAX_BET)
                else:
                    self.log.info("  AM MODE — P2: disabled")
            self.log.info("=" * 60)
            self._log_status_snapshot("BOT START")

            while True:
                self.pending_bet = 0.0   # clear any leftover from an aborted round
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
                self._update_drawdown_protection()
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

                # Wait for the betting window to open. In pure watch mode, let
                # a crash-history update advance the strategy even if the real
                # embed never exposes a matching BET button during this poll.
                self._set_phase("watching", f"Waiting for next round… [P1={next_pattern_state(p1_bet_plan)} P2={next_pattern_state(p2_bet_plan)}]")
                self.log.info("Waiting for bet phase… [P1=%s P2=%s]",
                              next_pattern_state(p1_bet_plan),
                              next_pattern_state(p2_bet_plan))
                observed_history = None
                try:
                    if not p1_bet_plan and not p1_assist_plan and not p2_bet_plan:
                        event, observed_history = await wait_for_bet_phase_or_history_change(
                            frame,
                            history,
                            timeout_s=15,
                        )
                        ok = event in ("bet", "history")
                    else:
                        event, observed_history = await wait_for_bet_phase(
                            frame,
                            timeout_s=15,
                            prev_history=history,
                        )
                        ok = event in ("bet", "history")
                        if event == "history":
                            if p1_bet_plan:
                                p1_bet_plan.pop(0)
                            if p1_assist_plan:
                                p1_assist_plan.pop(0)
                            if p2_bet_plan:
                                p2_bet_plan.pop(0)
                            self.log.warning(
                                "Bet window was missed before buttons became detectable — "
                                "consumed one planned step and continuing to watch."
                            )
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
                    probe = await frame_probe(frame)
                    self.log.warning(
                        "Bet phase/history not detected — continuing to watch. "
                        "frame=%s payouts=%s buttons=%s inputs=%s",
                        str(probe.get("url", frame.url))[:90],
                        probe.get("payouts"),
                        probe.get("buttons"),
                        probe.get("inputs"),
                    )
                    continue

                p1_this = p2_this = False
                p1_was_assisting = p2_was_assisting = False
                p1_recovery_leads_this = False
                p2_recovery_suppressed_this = False
                p1_cashout_this = self.PANEL1_CASHOUT
                p2_cashout_this = self.PANEL2_CASHOUT

                if observed_history is not None:
                    history = observed_history
                    crash_mult = history[0]
                else:
                    # Snapshot which panels are betting this round.
                    # Clean panels can assist the panel carrying debt, using the
                    # configured assist percentage instead of taking over all debt.
                    p1_scheduled_this  = p1_bet_plan.pop(0) if p1_bet_plan else False
                    p1_low_assist_this = p1_assist_plan.pop(0) if p1_assist_plan else False
                    p1_follow_this     = p1_follow_plan.pop(0) if p1_follow_plan else False
                    p1_low_zone_this   = p1_low_zone_plan.pop(0) if p1_low_zone_plan else False
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
                    p1_this = p1_scheduled_this or p1_assist_this or p1_follow_this or p1_low_zone_this
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
                    p1_cashout_this = (self.P1_ASSIST_CASHOUT if p1_was_assisting
                                       else getattr(self, "P1_LOW_ZONE_CASHOUT", 1.5) if p1_low_zone_this
                                       else self.PANEL1_CASHOUT)

                    # ── Set bet amounts for active panels ─────────────────────
                    try:
                        if p1_this:
                            if getattr(self, 'AM_STRATEGY_ENABLED', False):
                                _am_base = getattr(self, 'AM_BET_AMOUNT', 50.0)
                                _am_cap  = getattr(self, 'AM_MAX_BET', 5000.0)
                                _am_co   = getattr(self, 'AM_CASHOUT', 7.0)
                                self.p1_bet = min(max(_am_base, self._am_bet if self._am_bet > 0 else _am_base), _am_cap)
                                p1_cashout_this = _am_co
                                await self._setup_one_panel(frame, 0, _am_co, self.p1_bet)
                            elif p1_was_assisting:
                                self.p1_bet = self._p1_assist_p2_bet()
                                await self._setup_one_panel(frame, 0, self.P1_ASSIST_CASHOUT, self.p1_bet)
                            elif p1_low_zone_this:
                                _lz_pct  = getattr(self, "P1_LOW_ZONE_PERCENTAGE", 50)
                                _lz_co   = getattr(self, "P1_LOW_ZONE_CASHOUT", 1.5)
                                _lz_target = self.recovery_deficit * _lz_pct / 100
                                _lz_net_mult = max(0.01, _lz_co - 1)
                                self.p1_bet = (max(self.BET_AMOUNT, round((_lz_target + self.RECOVERY_PROFIT_TARGET) / _lz_net_mult, 2))
                                               if _lz_target > 0 else self.BET_AMOUNT)
                                await self._setup_one_panel(frame, 0, _lz_co, self.p1_bet)
                            elif p1_follow_this:
                                self.p1_bet = self.BET_AMOUNT
                            else:
                                p1_extra_risk = self.P2_BET_AMOUNT if p2_recovery_suppressed_this else 0.0
                                self.p1_bet = self._p1_bet(extra_risk=p1_extra_risk)
                            if self.p1_bet != self.BET_AMOUNT:
                                await self._set_panel1_bet(frame, self.p1_bet)
                        if p2_this:
                            if (getattr(self, 'AM_STRATEGY_ENABLED', False)
                                    and getattr(self, 'P2_AM_ENABLED', False)):
                                _p2_am_co   = getattr(self, 'P2_AM_CASHOUT', 8.0)
                                _p2_am_base = getattr(self, 'P2_AM_BET_AMOUNT', 50.0)
                                _p2_am_cap  = getattr(self, 'P2_AM_MAX_BET', 5000.0)
                                self.p2_bet = min(max(_p2_am_base, self._p2_am_bet if self._p2_am_bet > 0 else _p2_am_base), _p2_am_cap)
                                p2_cashout_this = _p2_am_co
                                await self._setup_one_panel(frame, 1, _p2_am_co, self.p2_bet)
                            else:
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

                    # ── Place bets for active panels ──────────────────────────
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
                        # Bets confirmed — deduct from live balance display immediately
                        self.pending_bet = round(
                            (self.p1_bet if p1_this else 0.0) + (self.p2_bet if p2_this else 0.0), 2
                        )
                        self.log.info("BET PLACED — %.2f KES at risk | live balance: %s",
                                      self.pending_bet, self._running_balance_text())

                    # ── Wait for round end ────────────────────────────────────
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
                        p1_bet_plan      = []
                        p1_assist_plan   = []
                        p1_follow_plan   = []
                        p1_low_zone_plan = []
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
                        p1_bet_plan      = []
                        p1_assist_plan   = []
                        p1_follow_plan   = []
                        p1_low_zone_plan = []
                        p2_bet_plan = []
                        continue

                    crash_mult = history[0]

                # ── Process results for betting panels ────────────────────────
                if p1_this or p2_this:
                    _peak_snap = self.peak_pnl   # snapshot before this round settles
                    p1_bet_used = self.p1_bet if p1_this else 0.0
                    p2_bet_used = self.p2_bet if p2_this else 0.0
                    round_pnl, desc = self._round_pnl(
                        crash_mult,
                        p1_bet_used,
                        p2_bet_used,
                        p1_cashout=p1_cashout_this,
                        p2_cashout=p2_cashout_this,
                    )
                    self.cumulative_pnl += round_pnl
                    self.pending_bet = 0.0   # bets settled — balance is live again
                    if self.STOP_PROFIT_LOSS_FRAC > 0 and _peak_snap >= self.STOP_ON_PROFIT:
                        _max_frac  = self.STOP_PROFIT_LOSS_FRAC_MAX
                        _target    = self.STOP_ON_PROFIT
                        _scale     = min(1.0, (_peak_snap - _target) / _target) if _target > 0 else 0.0
                        _eff_frac  = self.STOP_PROFIT_LOSS_FRAC + _scale * (_max_frac - self.STOP_PROFIT_LOSS_FRAC)
                        if round_pnl < 0 and abs(round_pnl) > _peak_snap * _eff_frac:
                            self.log.info(
                                "Profit protection: single round lost %.2f KES "
                                "(> %.0f%% of peak %.2f KES). Stopping.",
                                abs(round_pnl), _eff_frac * 100, _peak_snap,
                            )
                            self._set_phase("stopping", (
                                f"Profit protection — single bet lost {abs(round_pnl):.2f} KES "
                                f"(> {_eff_frac*100:.0f}% of peak {_peak_snap:.2f} KES)"
                            ))
                            break
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
                        if getattr(self, 'AM_STRATEGY_ENABLED', False):
                            if crash_mult >= p1_cashout_this:
                                self._am_streak += 1
                                _am_max_s = getattr(self, 'AM_MAX_STREAK', 4)
                                if self._am_streak >= _am_max_s:
                                    self._am_bet    = getattr(self, 'AM_BET_AMOUNT', 50.0)
                                    self._am_streak = 0
                                    self.log.info("AM WIN %.2fx — streak %d complete, reset to %.2f KES.",
                                                  crash_mult, _am_max_s, self._am_bet)
                                else:
                                    self._am_bet = min(self._am_bet * 2, getattr(self, 'AM_MAX_BET', 5000.0))
                                    self.log.info("AM WIN %.2fx — streak %d, next bet %.2f KES.",
                                                  crash_mult, self._am_streak, self._am_bet)
                                p1_bet_plan = p1_assist_plan = p1_follow_plan = p1_low_zone_plan = []
                                p1_session_pnl = 0.0
                                self._p1_consecutive_losses = 0
                                self._p1_cooldown = self.BURST_COOLDOWN
                            else:
                                self._am_bet    = getattr(self, 'AM_BET_AMOUNT', 50.0)
                                self._am_streak = 0
                                self.log.info("AM LOSS %.2fx — reset to base bet %.2f KES.", crash_mult, self._am_bet)
                                self._p1_consecutive_losses += 1
                                if (self.STOP_ON_CONSECUTIVE_LOSSES > 0
                                        and self._p1_consecutive_losses >= self.STOP_ON_CONSECUTIVE_LOSSES):
                                    self.log.warning("AM consecutive loss limit (%d) — stopping.", self._p1_consecutive_losses)
                                    self._last_stop_reason = f"AM consecutive loss limit ({self._p1_consecutive_losses})"
                                    break
                                if not p1_bet_plan:
                                    p1_session_pnl = 0.0
                                    self._p1_cooldown = self.BURST_COOLDOWN
                        elif crash_mult >= p1_cashout_this:
                            if p1_follow_this:
                                self.log.info("P1 FOLLOW WIN %.2fx — base bet won alongside P2.", crash_mult)
                            elif p1_low_zone_this:
                                _lz_gain = round(p1_bet_used * (p1_cashout_this - 1), 2)
                                self.recovery_deficit = max(0.0, round(self.recovery_deficit - _lz_gain, 2))
                                self.log.info("P1 LOW ZONE WIN %.2fx @ %.1fx — recovered %.2f KES, deficit %.2f KES.",
                                              crash_mult, p1_cashout_this, _lz_gain, self.recovery_deficit)
                            elif p1_was_assisting:
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
                                _covers_p2 = self.RECOVERY_SCOPE in ("combined", "smart")
                                _total_def  = self.recovery_deficit + (self.p2_recovery_deficit if _covers_p2 else 0.0)
                                _cap2       = self._effective_chunk_cap()
                                _chunk      = min(_total_def, _cap2) if _cap2 > 0 else _total_def
                                _leftover   = max(0.0, round(_total_def - _chunk, 2))
                                if _leftover > 0:
                                    self.log.info("P1 WIN %.2fx — recovered %.2f KES, %.2f KES deferred to next recovery.",
                                                  crash_mult, _chunk, _leftover)
                                else:
                                    self.log.info("P1 WIN %.2fx — deficit cleared (was %.2f KES).",
                                                  crash_mult, _total_def)
                                self.recovery_deficit = _leftover
                                if _covers_p2:
                                    self.p2_recovery_deficit = 0.0
                            self._p1_consecutive_losses = 0
                            p1_bet_plan      = []
                            p1_assist_plan   = []
                            p1_follow_plan   = []
                            p1_low_zone_plan = []
                            p1_session_pnl   = 0.0
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
                            if p1_follow_this:
                                self.recovery_deficit = round(self.recovery_deficit + self.p1_bet, 2)
                                self.log.info("P1 FOLLOW LOSS %.2fx — base bet lost → P1 deficit %.2f KES.",
                                              crash_mult, self.recovery_deficit)
                            elif p1_low_zone_this:
                                self.recovery_deficit = round(self.recovery_deficit + self.p1_bet, 2)
                                self.log.info("P1 LOW ZONE LOSS %.2fx — deficit %.2f KES.",
                                              crash_mult, self.recovery_deficit)
                            elif p1_was_assisting:
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
                                p1_bet_plan      = []
                                p1_assist_plan   = []
                                p1_follow_plan   = []
                                p1_low_zone_plan = []
                                p1_session_pnl   = 0.0
                                self._p1_cooldown = self.BURST_COOLDOWN
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
                        p2_session_pnl += p2_bet_used * (p2_cashout_this - 1) if crash_mult >= p2_cashout_this else -p2_bet_used
                        if (getattr(self, 'AM_STRATEGY_ENABLED', False)
                                and getattr(self, 'P2_AM_ENABLED', False)):
                            if crash_mult >= p2_cashout_this:
                                self._p2_am_streak += 1
                                _p2_am_max_s = getattr(self, 'P2_AM_MAX_STREAK', 4)
                                if self._p2_am_streak >= _p2_am_max_s:
                                    self._p2_am_bet    = getattr(self, 'P2_AM_BET_AMOUNT', 50.0)
                                    self._p2_am_streak = 0
                                    self.log.info("P2 AM WIN %.2fx — streak %d complete, reset to %.2f KES.",
                                                  crash_mult, _p2_am_max_s, self._p2_am_bet)
                                else:
                                    self._p2_am_bet = min(self._p2_am_bet * 2,
                                                          getattr(self, 'P2_AM_MAX_BET', 5000.0))
                                    self.log.info("P2 AM WIN %.2fx — streak %d, next bet %.2f KES.",
                                                  crash_mult, self._p2_am_streak, self._p2_am_bet)
                                p2_bet_plan = []
                                p2_session_pnl = 0.0
                                self._p2_consecutive_losses = 0
                                self._p2_cooldown = self.BURST_COOLDOWN
                            else:
                                self._p2_am_bet    = getattr(self, 'P2_AM_BET_AMOUNT', 50.0)
                                self._p2_am_streak = 0
                                self.log.info("P2 AM LOSS %.2fx — reset to base bet %.2f KES.", crash_mult, self._p2_am_bet)
                                self._p2_consecutive_losses += 1
                                if (self.STOP_ON_CONSECUTIVE_LOSSES > 0
                                        and self._p2_consecutive_losses >= self.STOP_ON_CONSECUTIVE_LOSSES):
                                    self.log.warning("P2 AM consecutive loss limit (%d) — stopping.", self._p2_consecutive_losses)
                                    self._last_stop_reason = f"P2 AM consecutive loss limit ({self._p2_consecutive_losses})"
                                    break
                                if not p2_bet_plan:
                                    p2_session_pnl = 0.0
                                    self._p2_cooldown = self.BURST_COOLDOWN
                        elif crash_mult >= p2_cashout_this:
                            if p2_recovery_suppressed_this:
                                self.log.info("P2 NORMAL WIN %.2fx — P1 recovery had priority; P2 deficit remains %.2f KES.",
                                              crash_mult, self.p2_recovery_deficit)
                            elif p2_was_assisting:
                                p2_net_gain = round(p2_bet_used * (p2_cashout_this - 1), 2)
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
                                    old_p1_def  = self.recovery_deficit
                                    old_p2_def  = self.p2_recovery_deficit
                                    total       = old_p1_def + old_p2_def
                                    _cap        = self._effective_chunk_cap()
                                    _chunk      = min(total, _cap) if _cap > 0 else total
                                    _leftover   = max(0.0, round(total - _chunk, 2))
                                    if _leftover > 0:
                                        self.log.info("P2 WIN %.2fx — recovered %.2f KES, %.2f KES deferred (chunk cap).",
                                                      crash_mult, _chunk, _leftover)
                                    else:
                                        self.log.info("P2 WIN %.2fx — combined deficit cleared (P1 %.2f, P2 %.2f).",
                                                      crash_mult, old_p1_def, old_p2_def)
                                    self.recovery_deficit = _leftover
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

                # ── Predictor: feed this round's result + print probs ────────
                if self._predictor is not None:
                    self._predictor.update(crash_mult)
                    if self._predictor.ready:
                        _recent_pred = list(reversed(history[:20]))
                        _am_on_pred  = getattr(self, 'AM_STRATEGY_ENABLED', False)
                        _p1_co = getattr(self, 'AM_CASHOUT', 7.0) if _am_on_pred else self.PANEL1_CASHOUT
                        _p2_co = (getattr(self, 'P2_AM_CASHOUT', 8.0)
                                  if (_am_on_pred and getattr(self, 'P2_AM_ENABLED', False))
                                  else self.PANEL2_CASHOUT)
                        _pp1   = self._predictor.get_prob_at(_p1_co, _recent_pred)
                        _pp2   = self._predictor.get_prob_at(_p2_co, _recent_pred)
                        _be1   = 1.0 / _p1_co
                        _be2   = 1.0 / _p2_co
                        _e1    = (_pp1 - _be1) / _be1 * 100
                        _e2    = (_pp2 - _be2) / _be2 * 100
                        self.log.info(
                            "PREDICTOR  P1(%.1fx)=%.3f [%+.0f%%]  P2(%.1fx)=%.3f [%+.0f%%]",
                            _p1_co, _pp1, _e1, _p2_co, _pp2, _e2,
                        )

                # ── Check triggers for each panel independently ───────────────
                _min_crash = getattr(self, "MIN_TRIGGER_CRASH", 0.0)
                if _min_crash > 0 and crash_mult < _min_crash:
                    self.log.info("GATE: crash %.2fx < MIN_TRIGGER_CRASH %.2fx — skipping all triggers.",
                                  crash_mult, _min_crash)
                if not p1_bet_plan and not (_min_crash > 0 and crash_mult < _min_crash):
                    if self._p1_cooldown > 0:
                        self._p1_cooldown -= 1
                        self.log.info("P1 cooldown: %d round(s) left.", self._p1_cooldown)
                    else:
                        if getattr(self, 'AM_STRATEGY_ENABLED', False):
                            _am_trig = getattr(self, 'AM_TRIGGER_CRASH', 8.0)
                            if crash_mult >= _am_trig:
                                if self._am_bet <= 0:
                                    self._am_bet = getattr(self, 'AM_BET_AMOUNT', 50.0)
                                self.log.info("AM TRIGGER — crash %.2fx ≥ %.1fx — next bet %.2f KES (streak %d).",
                                              crash_mult, _am_trig, self._am_bet, self._am_streak)
                                self._set_phase("triggered", f"AM TRIGGER: crash {crash_mult:.2f}x ≥ {_am_trig:.1f}x")
                                p1_bet_plan      = [True]
                                p1_assist_plan   = [False]
                                p1_low_zone_plan = [False]
                                p1_session_pnl   = 0.0
                            else:
                                self.log.info("AM WATCH | crash=%.2fx | trigger=%.1fx", crash_mult, _am_trig)
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
                            p1_trig_low_zone = (
                                self.P1_LOW_ZONE_ENABLED
                                and self.recovery_deficit > 0
                                and crash_mult <= self.P1_LOW_ZONE_MAX
                            )
                            if p1_trig_assist:
                                p1_reason = (
                                    f"P2 assist: crash {crash_mult:.2f}x <= {self.P1_ASSIST_TRIGGER_MAX:.1f}x "
                                    f"and P2 deficit {self.p2_recovery_deficit:.2f} KES"
                                )
                            elif p1_trig_high:
                                p1_reason = f"crash {crash_mult:.2f}x in ({self.P1_TRIGGER_MULT:.1f}x, {self.P1_TRIGGER_MULT_MAX:.1f}x]"
                            elif p1_trig_low:
                                p1_reason = f"last {self.P1_LOW_STREAK_COUNT} crashes all ≤ {self.P1_LOW_STREAK_MAX:.1f}x"
                            elif p1_trig_low_zone:
                                p1_reason = (
                                    f"LOW ZONE crash {crash_mult:.2f}x ≤ {self.P1_LOW_ZONE_MAX:.1f}x "
                                    f"— targeting {self.P1_LOW_ZONE_PERCENTAGE}% deficit "
                                    f"@ {self.P1_LOW_ZONE_CASHOUT:.1f}x"
                                )
                            else:
                                p1_reason = None
                            if p1_reason:
                                self._set_phase("triggered", f"P1 TRIGGER: {p1_reason} | {format_bet_pattern(self.P1_BET_PATTERN)}")
                                self.log.info("P1 TRIGGER (%s) — pattern %s", p1_reason, format_bet_pattern(self.P1_BET_PATTERN))
                                p1_bet_plan      = list(self.P1_BET_PATTERN)
                                p1_assist_plan   = [p1_trig_assist and bool(step) for step in p1_bet_plan]
                                p1_low_zone_plan = [p1_trig_low_zone and bool(step) for step in p1_bet_plan]
                                p1_session_pnl   = 0.0

                # ── Predictor confidence gate — P1 (recovery mode only) ──────
                if (p1_bet_plan
                        and self._predictor is not None
                        and self._predictor.ready
                        and not getattr(self, "AM_STRATEGY_ENABLED", False)):
                    _p1_conf = self.PREDICTOR_P1_CONFIDENCE
                    if _p1_conf > 0:
                        _recent = list(reversed(history[:20]))
                        _p_win  = self._predictor.get_prob_at(p1_cashout_this, _recent)
                        _min_p  = _p1_conf / p1_cashout_this
                        if 0 <= _p_win < _min_p:
                            self.log.info(
                                "PRED P1 ✗ SKIP  P(≥%.1fx)=%.3f  need≥%.3f  edge%+.0f%%",
                                p1_cashout_this, _p_win, _min_p,
                                (_p_win - _min_p) / _min_p * 100,
                            )
                            p1_bet_plan    = []
                            p1_session_pnl = 0.0
                        else:
                            self.log.info(
                                "PRED P1 ✓ BET   P(≥%.1fx)=%.3f  need≥%.3f  edge%+.0f%%",
                                p1_cashout_this, _p_win, _min_p,
                                (_p_win - _min_p) / _min_p * 100,
                            )

                if (not getattr(self, 'AM_STRATEGY_ENABLED', False)
                        and not p2_bet_plan
                        and not (_min_crash > 0 and crash_mult < _min_crash)):
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

                # ── Predictor confidence gate — P2 (recovery mode only) ──────
                if (p2_bet_plan
                        and self._predictor is not None
                        and self._predictor.ready
                        and not getattr(self, "AM_STRATEGY_ENABLED", False)
                        and not getattr(self, "P2_AM_ENABLED", False)):
                    _p2_conf = self.PREDICTOR_P2_CONFIDENCE
                    if _p2_conf > 0:
                        _recent = list(reversed(history[:20]))
                        _p_win  = self._predictor.get_prob_at(p2_cashout_this, _recent)
                        _min_p  = _p2_conf / p2_cashout_this
                        if 0 <= _p_win < _min_p:
                            self.log.info(
                                "PRED P2 ✗ SKIP  P(≥%.1fx)=%.3f  need≥%.3f  edge%+.0f%%",
                                p2_cashout_this, _p_win, _min_p,
                                (_p_win - _min_p) / _min_p * 100,
                            )
                            p2_bet_plan    = []
                            p2_session_pnl = 0.0
                        else:
                            self.log.info(
                                "PRED P2 ✓ BET   P(≥%.1fx)=%.3f  need≥%.3f  edge%+.0f%%",
                                p2_cashout_this, _p_win, _min_p,
                                (_p_win - _min_p) / _min_p * 100,
                            )

                # ── P2 Anti-Martingale trigger (AM mode only) ─────────────
                if (getattr(self, 'AM_STRATEGY_ENABLED', False)
                        and getattr(self, 'P2_AM_ENABLED', False)
                        and not p2_bet_plan
                        and not (_min_crash > 0 and crash_mult < _min_crash)):
                    if self._p2_cooldown > 0:
                        self._p2_cooldown -= 1
                    else:
                        _p2_am_trig = getattr(self, 'P2_AM_TRIGGER_CRASH', 8.0)
                        if crash_mult >= _p2_am_trig:
                            if self._p2_am_bet <= 0:
                                self._p2_am_bet = getattr(self, 'P2_AM_BET_AMOUNT', 50.0)
                            self.log.info("P2 AM TRIGGER — crash %.2fx ≥ %.1fx — next bet %.2f KES (streak %d).",
                                          crash_mult, _p2_am_trig, self._p2_am_bet, self._p2_am_streak)
                            self._set_phase("triggered", f"P2 AM TRIGGER: crash {crash_mult:.2f}x ≥ {_p2_am_trig:.1f}x")
                            p2_bet_plan    = [True]
                            p2_session_pnl = 0.0
                        else:
                            self.log.info("P2 AM WATCH | crash=%.2fx | trigger=%.1fx", crash_mult, _p2_am_trig)

                # ── Follow (idle-fill) logic ──────────────────────────────
                _gate_active = getattr(self, "MIN_TRIGGER_CRASH", 0.0) > 0 and crash_mult < self.MIN_TRIGGER_CRASH
                if not _gate_active and not getattr(self, 'AM_STRATEGY_ENABLED', False):
                    if p1_bet_plan and not p2_bet_plan and getattr(self, "P2_FOLLOW_P1", False):
                        p2_bet_plan = list(self.P2_BET_PATTERN)
                        self.log.info("P2 FOLLOW P1 — base bet at %.1fx alongside P1.", self.PANEL2_CASHOUT)
                    if p2_bet_plan and not p1_bet_plan and getattr(self, "P1_FOLLOW_P2", False):
                        p1_bet_plan    = list(self.P1_BET_PATTERN)
                        p1_follow_plan = [True] * len(p1_bet_plan)
                        self.log.info("P1 FOLLOW P2 — base bet at %.1fx alongside P2.", self.PANEL1_CASHOUT)

                # ── Session ended — restart or exit ───────────────────────────
                _explicit_stop = self._stop_event.is_set()
                self._print_session_summary()

                if _explicit_stop or not self.AUTO_RESTART_SESSION:
                    break  # exit session loop → proceed to finally

                _delay = self.RESTART_DELAY
                self.log.info("AUTO-RESTART: session %d done — next session in %d s…",
                              self.session_count, _delay)
                self._reset_session()
                if _delay > 0:
                    await asyncio.sleep(_delay)
                # session loop continues → open fresh game tab

        except KeyboardInterrupt:
            self.log.info("Interrupted by user.")
            self._last_stop_reason = "Interrupted by user (Ctrl+C)"
        except Exception as e:
            self.log.exception("Unhandled error: %s", e)
            self._last_stop_reason = f"Unhandled error: {e}"
        finally:
            self._print_session_summary()
            self._print_summary()
            self.csv.close()
            if self.AUTO_LOGOUT:
                await self.logout()
            else:
                self.log.info("Auto-logout disabled — staying logged in.")
                self._set_phase("stopped", "Bot stopped (still logged in)")
            await self.stop()

    def _reset_session(self):
        """Reset per-session state for auto-restart. Lifetime totals are preserved."""
        self.lifetime_pnl          += self.cumulative_pnl
        self.cumulative_pnl         = 0.0
        self.peak_pnl               = 0.0
        self.recovery_deficit       = 0.0
        self.p2_recovery_deficit    = 0.0
        self.drawdown_protection_active = False
        self._drawdown_threshold_kes    = 0.0
        self._am_bet    = 0.0
        self._am_streak = 0
        self._p2_am_bet    = 0.0
        self._p2_am_streak = 0
        self.p1_bet                 = self.BET_AMOUNT
        self.p2_bet                 = self.P2_BET_AMOUNT
        self._p1_consecutive_losses = 0
        self._p2_consecutive_losses = 0
        self._p1_cooldown           = 0
        self._p2_cooldown           = 0
        self._p1_step               = 0
        self._p2_step               = 0

    def _write_session_report(self, end_time: datetime):
        """Append a full session report to reports/sessions_YYYYMMDD.txt."""
        os.makedirs("reports", exist_ok=True)
        path = os.path.join("reports", f"sessions_{end_time.strftime('%Y%m%d')}.txt")

        rate     = (self.total_wins / self.total_rounds * 100) if self.total_rounds else 0
        lifetime = self.lifetime_pnl + self.cumulative_pnl
        start    = self.session_start_time or end_time
        duration = end_time - start
        mins, secs = divmod(int(duration.total_seconds()), 60)
        hrs,  mins = divmod(mins, 60)
        dur_str  = (f"{hrs}h {mins}m {secs}s" if hrs else f"{mins}m {secs}s")

        W = 56
        sep  = "=" * W
        dash = "-" * W

        lines = [
            "",
            sep,
            f"  SESSION #{self.session_count} REPORT",
            sep,
            f"  Started      : {start.strftime('%Y-%m-%d  %H:%M:%S')}",
            f"  Ended        : {end_time.strftime('%Y-%m-%d  %H:%M:%S')}",
            f"  Duration     : {dur_str}",
            f"  Stop reason  : {self._last_stop_reason}",
            dash,
            f"  Net P&L      : KES {self.cumulative_pnl:+,.2f}",
            f"  Peak P&L     : KES +{self.peak_pnl:,.2f}",
            dash,
            f"  Rounds bet   : {self.total_rounds}",
            f"  Wins         : {self.total_wins}  ({rate:.1f}%)",
            f"  Losses       : {self.total_losses}  ({100-rate:.1f}%)",
            dash,
            f"  P1 deficit   : KES {self.recovery_deficit:,.2f}",
            f"  P2 deficit   : KES {self.p2_recovery_deficit:,.2f}",
        ]
        if self.session_count > 1 or self.AUTO_RESTART_SESSION:
            lines.append(f"  Lifetime PnL : KES {lifetime:+,.2f}")
        lines += [
            dash,
            f"  Strategy",
            f"    P1 trigger : > {self.P1_TRIGGER_MULT:.1f}×  cashout {self.PANEL1_CASHOUT:.1f}×",
            f"    P2 trigger : ≤ {self.P2_LOW_STREAK_MAX:.1f}× × {self.P2_LOW_STREAK_COUNT}  cashout {self.PANEL2_CASHOUT:.1f}×",
            f"    Stop profit: KES {self.STOP_ON_PROFIT:,.0f}",
            f"    Stop loss  : {'KES ' + f'{self.STOP_ON_LOSS:,.0f}' if self.STOP_ON_LOSS < 0 else 'disabled'}",
            sep,
            "",
        ]

        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        self.log.info("Session report saved → %s", os.path.abspath(path))

    def _print_session_summary(self):
        """Print a concise summary for the session that just ended."""
        end_time = datetime.now()
        rate     = (self.total_wins / self.total_rounds * 100) if self.total_rounds else 0
        lifetime = self.lifetime_pnl + self.cumulative_pnl
        pnl_tag  = f"KES {self.cumulative_pnl:+.2f}"

        # ── terminal print (stands out from log lines) ────────────────────────
        print()
        print("┌" + "─" * 50 + "┐")
        print(f"│  SESSION {self.session_count} COMPLETE" + " " * (40 - len(str(self.session_count))) + "│")
        print("├" + "─" * 50 + "┤")
        print(f"│  Net P&L      : {pnl_tag:<33}│")
        print(f"│  Peak P&L     : KES +{self.peak_pnl:<27.2f}│")
        print(f"│  Rounds bet   : {self.total_rounds:<33}│")
        print(f"│  Wins/Losses  : {self.total_wins}/{self.total_losses}  ({rate:.1f}% win rate)" +
              " " * max(0, 27 - len(f"{self.total_wins}/{self.total_losses}  ({rate:.1f}% win rate)")) + "│")
        print(f"│  P1 deficit   : KES {self.recovery_deficit:<29.2f}│")
        print(f"│  P2 deficit   : KES {self.p2_recovery_deficit:<29.2f}│")
        if self.session_count > 1 or self.AUTO_RESTART_SESSION:
            print(f"│  Lifetime PnL : KES {lifetime:+<29.2f}│")
        print("└" + "─" * 50 + "┘")
        print()

        # ── write report file ─────────────────────────────────────────────────
        self._write_session_report(end_time)

        # ── also log (goes to log file) ────────────────────────────────────────
        self.log.info("SESSION %d COMPLETE — PnL %+.2f KES | Peak +%.2f KES | "
                      "%d rounds | %d wins / %d losses",
                      self.session_count, self.cumulative_pnl, self.peak_pnl,
                      self.total_rounds, self.total_wins, self.total_losses)

    def _print_summary(self):
        self.log.info("=" * 60)
        self.log.info("FINAL SUMMARY — %d session(s)", self.session_count)
        self.log.info("  Rounds bet    : %d", self.total_rounds)
        self.log.info("  Wins          : %d", self.total_wins)
        self.log.info("  Losses        : %d", self.total_losses)
        rate = (self.total_wins / self.total_rounds * 100) if self.total_rounds else 0
        self.log.info("  Win rate      : %.1f%%", rate)
        self.log.info("  Last session  : KES %+.2f", self.cumulative_pnl)
        total = self.lifetime_pnl + self.cumulative_pnl
        if self.session_count > 1:
            self.log.info("  Lifetime PnL  : KES %+.2f", total)
        self._log_status_snapshot("FINAL")
        self.log.info("=" * 60)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    await AviatorBot().run()


if __name__ == "__main__":
    asyncio.run(main())
