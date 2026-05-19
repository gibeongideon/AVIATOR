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

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"aviator_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"),
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


def calc_p1_bet(p1_deficit: float, p2_deficit: float = 0.0, step: int = 0) -> float:
    if not config.RECOVERY_ENABLED:
        return config.BET_AMOUNT
    if config.RECOVERY_SCOPE == "individual":
        if p1_deficit > 0:
            target = p1_deficit
        elif config.P1_ASSIST_P2_ENABLED and p2_deficit > 0:
            target = p2_deficit * config.P1_ASSIST_PERCENTAGE / 100
        else:
            target = 0.0
    elif config.RECOVERY_SCOPE in ("combined", "smart"):
        target = p1_deficit + p2_deficit   # P1 is the big gun — covers everything
    else:  # "percentage"
        total = p1_deficit + p2_deficit
        max_steps = config.RECOVERY_STEPS if config.RECOVERY_STEPS > 0 else config.P1_MAX_BET_ROUNDS
        is_last = (step + 1) >= max_steps
        target = total if is_last else total * config.RECOVERY_PERCENTAGE / 100
    if target <= 0:
        return config.BET_AMOUNT
    return max(config.BET_AMOUNT,
               round((target + config.RECOVERY_PROFIT_TARGET) / config.PANEL1_CASHOUT, 2))


def calc_p2_bet(p1_deficit: float, p2_deficit: float, step: int = 0) -> float:
    if not config.P2_RECOVERY_ENABLED:
        return config.P2_BET_AMOUNT
    if p1_deficit > 0:
        # P1 is recovering — P2 either assists or bets minimum; assist can run even if P2 also has deficit
        if config.P2_ASSIST_P1_ENABLED:
            assist_target = p1_deficit * config.P2_ASSIST_PERCENTAGE / 100
            return max(config.P2_BET_AMOUNT,
                       round((assist_target + config.P2_RECOVERY_PROFIT_TARGET) / config.PANEL2_CASHOUT, 2))
        return config.P2_BET_AMOUNT
    # P1 is clean — P2 runs its own independent recovery
    if config.P2_RECOVERY_SCOPE in ("individual", "smart"):
        target = p2_deficit
    elif config.P2_RECOVERY_SCOPE == "combined":
        target = p1_deficit + p2_deficit
    else:  # "percentage"
        total = p1_deficit + p2_deficit
        max_steps = config.P2_RECOVERY_STEPS if config.P2_RECOVERY_STEPS > 0 else config.P2_MAX_BET_ROUNDS
        is_last = (step + 1) >= max_steps
        target = total if is_last else total * config.P2_RECOVERY_PERCENTAGE / 100
    if target <= 0:
        return config.P2_BET_AMOUNT
    return max(config.P2_BET_AMOUNT,
               round((target + config.P2_RECOVERY_PROFIT_TARGET) / config.PANEL2_CASHOUT, 2))


def calc_round_pnl(crash_mult: float, p1_bet: float, p2_bet: float) -> tuple[float, str]:
    p1_win = crash_mult >= config.PANEL1_CASHOUT
    p2_win = crash_mult >= config.PANEL2_CASHOUT
    pnl = 0.0
    pnl += p1_bet * (config.PANEL1_CASHOUT - 1) if p1_win else -p1_bet
    pnl += p2_bet * (config.PANEL2_CASHOUT - 1) if p2_win else -p2_bet
    p1_tag = f"WIN@{config.PANEL1_CASHOUT:.0f}x" if p1_win else "LOSS"
    p2_tag = f"WIN@{config.PANEL2_CASHOUT:.0f}x" if p2_win else "LOSS"
    desc = f"P1={p1_tag}(bet={p1_bet})  P2={p2_tag}(bet={p2_bet})  crash={crash_mult:.2f}x"
    return pnl, desc


# ── CSV history writer ────────────────────────────────────────────────────────

class HistoryCSV:
    """
    Appends every round to a CSV for AI training.

    Columns:
      timestamp         — ISO-8601 local time the round ended
      crash_mult        — the multiplier at which the plane crashed (e.g. 3.45)
      round_pnl         — profit/loss change from this round only
      bankroll_change   — running cumulative P&L from the session start
      total_win         — legacy alias for cumulative P&L
      highest_positive  — highest cumulative positive move reached so far
      lowest_negative   — deepest cumulative negative move reached so far
    """

    COLUMNS = [
        "timestamp",
        "crash_mult",
        "round_pnl",
        "bankroll_change",
        "total_win",
        "highest_positive",
        "lowest_negative",
    ]

    def __init__(self):
        os.makedirs("history", exist_ok=True)
        date_str = datetime.now().strftime("%Y%m%d")
        base_path = os.path.join("history", f"aviator_{date_str}.csv")
        self.path = base_path
        write_header = not os.path.exists(self.path)
        if not write_header:
            try:
                with open(self.path, "r", newline="", encoding="utf-8") as existing_fh:
                    header = next(csv.reader(existing_fh), [])
                if header != self.COLUMNS:
                    self.path = os.path.join("history", f"aviator_{date_str}_v2.csv")
                    write_header = not os.path.exists(self.path)
            except Exception:
                self.path = os.path.join("history", f"aviator_{date_str}_v2.csv")
                write_header = not os.path.exists(self.path)
        self._fh  = open(self.path, "a", newline="", encoding="utf-8")
        self._csv = csv.DictWriter(self._fh, fieldnames=self.COLUMNS)
        if write_header:
            self._csv.writeheader()
        log.info("History CSV: %s", os.path.abspath(self.path))

    def record(
        self,
        crash_mult: float,
        round_pnl: float = 0.0,
        total_win: float = 0.0,
        highest_positive: float = 0.0,
        lowest_negative: float = 0.0,
    ):
        self._csv.writerow({
            "timestamp":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "crash_mult":       f"{crash_mult:.2f}",
            "round_pnl":        f"{round_pnl:.2f}",
            "bankroll_change":  f"{total_win:.2f}",
            "total_win":        f"{total_win:.2f}",
            "highest_positive": f"{highest_positive:.2f}",
            "lowest_negative":  f"{lowest_negative:.2f}",
        })
        self._fh.flush()

    def close(self):
        self._fh.close()


# ── Bot ───────────────────────────────────────────────────────────────────────

class AviatorBot:

    def __init__(self):
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page:    Optional[Page]    = None

        # Totals
        self.total_rounds = 0
        self.total_wins   = 0
        self.total_losses = 0
        self.cumulative_pnl = 0.0
        self.highest_positive_pnl = 0.0
        self.lowest_negative_pnl  = 0.0

        self.recovery_deficit    = 0.0
        self.p2_recovery_deficit = 0.0
        self.p1_bet = config.BET_AMOUNT
        self.p2_bet = config.P2_BET_AMOUNT
        self.DEMO_MODE   = config.DEMO_MODE
        self.AUTO_LOGOUT = config.AUTO_LOGOUT

        self._p1_consecutive_losses = 0
        self._p2_consecutive_losses = 0
        self._p1_cooldown           = 0
        self._p2_cooldown           = 0
        self._p1_step               = 0   # persistent pct-recovery step for P1
        self._p2_step               = 0   # persistent pct-recovery step for P2
        self._demo_reconnects       = 0   # how many times we have reopened the demo tab

        self.csv = HistoryCSV()

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

    def _running_balance_text(self) -> str:
        initial_demo_balance = getattr(config, "INITIAL_DEMO_BALANCE", None)
        if self.DEMO_MODE and initial_demo_balance not in (None, 0, 0.0, ""):
            return f"{float(initial_demo_balance) + self.cumulative_pnl:,.2f} KES"
        return f"P&L {self.cumulative_pnl:+.2f} KES"

    def _update_pnl_extremes(self):
        self.highest_positive_pnl = max(self.highest_positive_pnl, self.cumulative_pnl)
        self.lowest_negative_pnl = min(self.lowest_negative_pnl, self.cumulative_pnl)

    # ── Browser ───────────────────────────────────────────────────────────────

    async def start(self):
        pw = await async_playwright().start()
        self.browser = await pw.chromium.launch(
            headless=config.HEADLESS,
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

    async def stop(self):
        if self.browser:
            await self.browser.close()

    # ── Login ─────────────────────────────────────────────────────────────────

    async def login(self):
        log.info("Logging in…")
        await self.page.goto(config.LOGIN_URL, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(2000)
        await self.page.fill(SEL["login_user"], config.USERNAME)
        await self.page.fill(SEL["login_pass"], config.PASSWORD)
        await self.page.click(SEL["login_btn"])
        try:
            await self.page.wait_for_url(lambda u: "login" not in u, timeout=15_000)
            log.info("Login successful.")
            await self._dismiss_page_popups()
        except PWTimeout:
            log.error("Login may have failed — still on login page.")
            raise

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
                        log.info("Dismissed popup: %s", sel)
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
                        log.info("Demo clicked in casino-frontend frame (attempt %d).", attempt + 1)
                        return True
                except Exception as exc:
                    log.debug("Demo click attempt %d: %s", attempt + 1, exc)
            await asyncio.sleep(0.5)
        log.info("Demo button not found in casino-frontend frame — Spribe fallback will run.")
        return False

    async def _select_demo_mode(self, frame):
        """Click Demo/Try for Free if the Spribe mode-selection screen appears."""
        await asyncio.sleep(0.8)
        for sel in [
            'button:has-text("Demo")',
            'button:has-text("Try for free")',
            'button:has-text("Try For Free")',
            'button:has-text("Fun")',
            'button:has-text("Practice")',
            '[data-testid="demo-button"]',
            '[class*="demo-btn"]', '[class*="fun-btn"]',
        ]:
            try:
                el = await frame.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    await asyncio.sleep(1.0)
                    log.info("Demo mode selected via: %s", sel)
                    return
            except Exception:
                continue
        log.info("No Demo mode selector found in frame.")

    # ── Open game ─────────────────────────────────────────────────────────────

    def _get_frame(self):
        # Demo mode: self.page IS the spribegaming tab — main frame is the game
        if "spribegaming.com" in self.page.url or "aviator-next" in self.page.url:
            return self.page.main_frame
        # SportPesa mode: game runs inside an iframe
        for f in self.page.frames:
            if "spribegaming.com" in f.url or "aviator-next" in f.url:
                return f
        return None

    async def _wait_for_frame(self, timeout_s=30):
        demo_attempted = False
        for _ in range(timeout_s * 2):
            frame = self._get_frame()
            if frame:
                try:
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
        log.info("Opening Spribe demo (no login required)…")
        demo_page = await self.context.new_page()

        # Track new tabs spawned by the demo page
        new_tabs: list = []
        self.context.on("page", lambda pg: new_tabs.append(pg))

        await demo_page.goto("https://spribe.co/games/aviator", wait_until="domcontentloaded")
        await demo_page.wait_for_timeout(2000)

        try:
            await demo_page.click("button:has-text('Got it')", timeout=3000)
            log.info("Cookie banner dismissed.")
        except Exception:
            pass

        await demo_page.click('a.demo-link button, button.btn-demo', timeout=10_000)
        log.info("Play Demo clicked.")
        await demo_page.wait_for_timeout(1000)

        await demo_page.click("button:has-text('Yes')", timeout=8_000)
        log.info("Age confirmed.")

        log.info("Waiting for demo game tab (spribegaming.com) to open…")
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
        log.info("Demo tab: %s", game_tab.url[:90])

        await demo_page.close()       # close marketing page, keep game tab
        self.page = game_tab           # swap — all bot methods now act on game tab

        log.info("Waiting for demo game inputs…")
        frame = await self._wait_for_frame(timeout_s=45)
        log.info("Demo game ready.")
        return frame

    async def _reconnect_demo(self):
        """
        Called when the demo tab drops or freezes.
        Closes the stale tab, reopens a fresh Spribe demo session, re-sets
        up panels, and returns the new frame.  All deficit/PnL state is kept.
        """
        self._demo_reconnects += 1
        log.warning("Demo connection lost — reconnecting (attempt %d)…", self._demo_reconnects)
        try:
            await self.page.close()
        except Exception:
            pass
        frame = await self.open_aviator_demo()
        await self.setup_panels(frame)
        log.info("Reconnected. Deficits preserved — P1=%.2f  P2=%.2f",
                 self.recovery_deficit, self.p2_recovery_deficit)
        return frame

    async def _recover_runtime(self, reason: str = "runtime not alive"):
        """
        Recreate the browser/game runtime and continue with the same bot state.
        Keeps PnL/deficits/history intact while rebuilding the page/frame.
        """
        log.warning("Runtime recovery started: %s", reason)

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
        self._demo_reconnects += 1
        log.info("Runtime recovered successfully.")
        return frame

    async def open_aviator(self):
        log.info("Opening Aviator…")
        await self.page.goto(config.AVIATOR_URL, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(2000)
        try:
            await self.page.click(SEL["cookie_accept"], timeout=4_000)
            log.info("Cookie banner dismissed.")
        except PWTimeout:
            pass
        await self._dismiss_page_popups()
        log.info("Waiting for Spribe game frame + inputs…")
        frame = await self._wait_for_frame(timeout_s=45)
        log.info("Game ready: %s", frame.url[:70])
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
            log.warning("Panel %d Auto tab not found (only %d tabs)", panel_idx, len(auto_tabs))
            return
        auto_tab = auto_tabs[panel_idx]

        # Click Auto tab if not already active
        cls = await auto_tab.get_attribute("class") or ""
        if "active" not in cls:
            await auto_tab.click()
            await asyncio.sleep(0.5)
            log.info("  Panel %d: clicked Auto tab.", panel_idx)

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
                    log.info("  Panel %d: Auto Cash Out toggle enabled.", panel_idx)
                else:
                    log.info("  Panel %d: Auto Cash Out toggle already ON.", panel_idx)
        else:
            log.warning("  Panel %d: cash-out-switcher not found.", panel_idx)

        # Find cashout inputs directly — one per wrapper, no dedup needed
        spinner_inputs = []
        for inp in await frame.query_selector_all('.cashout-spinner-wrapper input'):
            if await inp.is_visible():
                spinner_inputs.append(inp)

        if panel_idx < len(spinner_inputs):
            inp = spinner_inputs[panel_idx]
            cur = await inp.input_value()
            log.info("  Panel %d: cashout input found (current=%r). Setting to %s…", panel_idx, cur, cashout_target)
            await set_input(inp, cashout_target)
            after = await inp.input_value()
            log.info("  Panel %d: cashout value is now %r", panel_idx, after)
        else:
            log.warning("  Panel %d: cashout spinner input not found (%d found).", panel_idx, len(spinner_inputs))

        # Set the bet amount
        _bet = bet_amount if bet_amount is not None else config.BET_AMOUNT
        bet_inputs = await frame.query_selector_all(SEL["bet_inputs"])
        if panel_idx < len(bet_inputs):
            await set_input(bet_inputs[panel_idx], _bet)
            log.info("  Panel %d: bet amount set to %s KES.", panel_idx, _bet)

    async def setup_panels(self, frame):
        """
        Set up both panels:
          - Auto tab → enables Auto Cash Out toggle
          - Auto Cash Out toggle ON → reveals cashout odds input
          - Panel 1 cashout: PANEL1_CASHOUT (6x)
          - Panel 2 cashout: PANEL2_CASHOUT (3x)
          - Both bets: BET_AMOUNT (1 KES)
        """
        log.info("Setting up Panel 1 (cashout=%.1fx, bet=%s KES)…",
                 config.PANEL1_CASHOUT, config.BET_AMOUNT)
        await self._setup_one_panel(frame, panel_idx=0,
                                    cashout_target=config.PANEL1_CASHOUT,
                                    bet_amount=config.BET_AMOUNT)

        log.info("Setting up Panel 2 (cashout=%.1fx, bet=%s KES)…",
                 config.PANEL2_CASHOUT, config.P2_BET_AMOUNT)
        await self._setup_one_panel(frame, panel_idx=1,
                                    cashout_target=config.PANEL2_CASHOUT,
                                    bet_amount=config.P2_BET_AMOUNT)

        # ── Verify all visible inputs ─────────────────────────────────────────
        await asyncio.sleep(0.4)
        visible_vals = []
        for inp in await frame.query_selector_all('input'):
            if await inp.is_visible():
                visible_vals.append(await inp.input_value())
        log.info("Visible input values after setup: %s", visible_vals)
        log.info("Setup complete — P1 bet=1 @%.1fx | P2 bet=1 @%.1fx",
                 config.PANEL1_CASHOUT, config.PANEL2_CASHOUT)

    # ── Panel 1 martingale bet update ─────────────────────────────────────────

    async def _set_panel1_bet(self, frame, amount: float):
        bet_inputs = await frame.query_selector_all(SEL["bet_inputs"])
        if bet_inputs:
            await set_input(bet_inputs[0], amount)
            log.info("P1 bet → %.2f KES (P1 deficit: %.2f KES).", amount, self.recovery_deficit)

    async def _set_panel2_bet(self, frame, amount: float):
        bet_inputs = await frame.query_selector_all(SEL["bet_inputs"])
        if len(bet_inputs) > 1:
            await set_input(bet_inputs[1], amount)
            log.info("P2 bet → %.2f KES (P2 deficit: %.2f KES).", amount, self.p2_recovery_deficit)

    # ── Place bets on both panels ─────────────────────────────────────────────

    async def place_bets(self, frame, p1: bool = True, p2: bool = True) -> bool:
        btns = await frame.query_selector_all(SEL["bet_btn"])
        if not btns:
            log.warning("BET buttons not found — bet phase may have already closed.")
            return False
        placed = False
        if p1 and len(btns) > 0:
            await btns[0].click()
            placed = True
        if p2 and len(btns) > 1:
            await asyncio.sleep(0.1)
            await btns[1].click()
            placed = True
        log.info("Bets placed — P1=%s P2=%s.", p1, p2)
        return placed

    # ── Global stop checks ────────────────────────────────────────────────────

    def should_stop(self) -> Optional[str]:
        if self.cumulative_pnl >= config.STOP_ON_PROFIT:
            return f"Profit target reached (KES {self.cumulative_pnl:.2f})"
        if self.cumulative_pnl <= config.STOP_ON_LOSS:
            return f"Loss limit hit (KES {self.cumulative_pnl:.2f})"
        return None

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self):
        await self.start()
        _restarts   = 0
        _MAX_RESTARTS = 20  # demo only — each restart reopens spribe.co

        try:
            while True:   # outer restart loop — demo mode only
                try:
                    if self.DEMO_MODE:
                        frame = await self.open_aviator_demo()   # no login needed
                    else:
                        await self.login()
                        frame = await self.open_aviator()
                    await self.setup_panels(frame)

                    # ── Per-panel independent state ───────────────────────────
                    p1_bet_plan    = []
                    p1_session_pnl = 0.0

                    p2_bet_plan    = []
                    p2_session_pnl = 0.0
                    p1_pattern = normalize_bet_pattern(getattr(config, "P1_BET_PATTERN", None), config.P1_MAX_BET_ROUNDS)
                    p2_pattern = normalize_bet_pattern(getattr(config, "P2_BET_PATTERN", None), config.P2_MAX_BET_ROUNDS)

                    history = await get_crash_history(frame)

                    log.info("=" * 60)
                    log.info("Strategy active — INDEPENDENT TRIGGERS")
                    log.info("  P1: trigger > %.1fx | low ≤%.1fx × %d | pattern %s | cashout %.1fx",
                             config.P1_TRIGGER_MULT, config.P1_LOW_STREAK_MAX,
                             config.P1_LOW_STREAK_COUNT, format_bet_pattern(p1_pattern), config.PANEL1_CASHOUT)
                    log.info("  P2: trigger > %.1fx | low ≤%.1fx × %d | pattern %s | cashout %.1fx",
                             config.P2_TRIGGER_MULT, config.P2_LOW_STREAK_MAX,
                             config.P2_LOW_STREAK_COUNT, format_bet_pattern(p2_pattern), config.PANEL2_CASHOUT)
                    log.info("  Stop profit KES %.0f | Stop loss KES %.0f",
                             config.STOP_ON_PROFIT, config.STOP_ON_LOSS)
                    log.info("=" * 60)

                    while True:
                        # Global guard
                        reason = self.should_stop()
                        if reason:
                            log.info("Bot stopping: %s", reason)
                            break
                        if not self._runtime_alive():
                            try:
                                frame = await self._recover_runtime("browser/page not alive")
                                continue
                            except Exception as e:
                                log.error("Runtime recovery failed: %s — aborting.", e)
                                break

                        # Always use a fresh frame reference — the iframe reloads periodically
                        frame = self._get_frame()
                        if frame is None:
                            log.warning("Game frame lost — waiting for it to reload…")
                            try:
                                frame = await self._wait_for_frame(timeout_s=15)
                                log.info("Frame recovered.")
                            except TimeoutError:
                                if config.DEMO_MODE:
                                    try:
                                        frame = await self._reconnect_demo()
                                    except Exception as e:
                                        log.error("Reconnect failed: %s — aborting.", e)
                                        break
                                else:
                                    log.error("Frame never came back — aborting.")
                                    break

                        # Wait for the betting window to open
                        log.info("Waiting for bet phase… [P1=%s P2=%s]",
                                 next_pattern_state(p1_bet_plan),
                                 next_pattern_state(p2_bet_plan))
                        try:
                            ok = await wait_for_bet_phase(frame, timeout_s=2)
                        except Exception as e:
                            log.warning("Frame context lost during bet-phase wait (%s) — reconnecting.", e)
                            if config.DEMO_MODE:
                                try:
                                    frame = await self._reconnect_demo()
                                    continue
                                except Exception as re:
                                    log.error("Reconnect failed: %s — aborting.", re)
                                    break
                            else:
                                continue
                        if not ok:
                            if not self._runtime_alive():
                                try:
                                    frame = await self._recover_runtime("browser/page closed during bet wait")
                                    continue
                                except Exception as e:
                                    log.error("Runtime recovery failed: %s — aborting.", e)
                                    break
                            if config.DEMO_MODE:
                                continue
                            else:
                                log.error("Bet phase never opened — aborting.")
                                break

                        # Snapshot which panels are betting this round.
                        # Clean panels can assist the panel carrying debt, using the
                        # configured assist percentage instead of taking over all debt.
                        p1_scheduled_this = p1_bet_plan.pop(0) if p1_bet_plan else False
                        p2_scheduled_this = p2_bet_plan.pop(0) if p2_bet_plan else False
                        p2_assist_this = (
                            p1_scheduled_this
                            and self.recovery_deficit > 0
                            and config.P2_ASSIST_P1_ENABLED
                            and config.P2_RECOVERY_ENABLED
                        )
                        p1_assist_this = (
                            p2_scheduled_this
                            and config.RECOVERY_SCOPE == "individual"
                            and config.P1_ASSIST_P2_ENABLED
                            and config.RECOVERY_ENABLED
                            and self.recovery_deficit <= 0
                            and self.p2_recovery_deficit > 0
                        )
                        p1_this = p1_scheduled_this or p1_assist_this
                        p2_this = p2_scheduled_this or p2_assist_this
                        p1_was_assisting = (
                            p1_this
                            and config.RECOVERY_SCOPE == "individual"
                            and config.P1_ASSIST_P2_ENABLED
                            and config.RECOVERY_ENABLED
                            and self.recovery_deficit <= 0
                            and self.p2_recovery_deficit > 0
                        )
                        p2_was_assisting = p2_assist_this

                        # ── Set bet amounts for active panels ─────────────────────────
                        try:
                            if p1_this:
                                self.p1_bet = calc_p1_bet(self.recovery_deficit, self.p2_recovery_deficit, self._p1_step)
                                if self.p1_bet != config.BET_AMOUNT:
                                    await self._set_panel1_bet(frame, self.p1_bet)
                            if p2_this:
                                self.p2_bet = calc_p2_bet(self.recovery_deficit, self.p2_recovery_deficit, self._p2_step)
                                if self.p2_bet != config.P2_BET_AMOUNT:
                                    await self._set_panel2_bet(frame, self.p2_bet)
                        except Exception as e:
                            log.warning("Frame stale setting bets (%s) — skipping round.", e)
                            if self.DEMO_MODE:
                                try:
                                    frame = await self._reconnect_demo()
                                except Exception as re:
                                    log.error("Reconnect failed: %s — aborting.", re)
                                    break
                            continue

                        prev_history = await get_crash_history(frame)

                        # ── Place bets for active panels ──────────────────────────────
                        if p1_this or p2_this:
                            try:
                                placed = await self.place_bets(frame, p1=p1_this, p2=p2_this)
                            except Exception as e:
                                log.warning("Frame stale placing bet (%s) — skipping round.", e)
                                if self.DEMO_MODE:
                                    try:
                                        frame = await self._reconnect_demo()
                                    except Exception as re:
                                        log.error("Reconnect failed: %s — aborting.", re)
                                        break
                                continue
                            if not placed:
                                log.warning("Could not place bets — skipping round.")
                                continue

                        # ── Wait for round end ────────────────────────────────────────
                        try:
                            history = await wait_for_round_end(frame, prev_history)
                        except TimeoutError:
                            if self.DEMO_MODE:
                                log.warning("Round end timeout — reconnecting demo and continuing.")
                                try:
                                    frame = await self._reconnect_demo()
                                    continue
                                except Exception as e:
                                    log.error("Reconnect failed: %s — aborting.", e)
                                    break
                            log.error("Round end timeout — resetting both panels to watch.")
                            p1_bet_plan = []
                            p2_bet_plan = []
                            continue
                        except Exception as e:
                            if self.DEMO_MODE:
                                log.warning("Frame stale waiting for round end (%s) — reconnecting demo.", e)
                                try:
                                    frame = await self._reconnect_demo()
                                    continue
                                except Exception as re:
                                    log.error("Reconnect failed: %s — aborting.", re)
                                    break
                            log.warning("Frame stale waiting for round end (%s) — resetting.", e)
                            p1_bet_plan = []
                            p2_bet_plan = []
                            continue

                        crash_mult = history[0]

                        # ── Process results for betting panels ────────────────────────
                        if p1_this or p2_this:
                            p1_bet_used = self.p1_bet if p1_this else 0.0
                            p2_bet_used = self.p2_bet if p2_this else 0.0
                            round_pnl, desc = calc_round_pnl(crash_mult, p1_bet_used, p2_bet_used)
                            self.cumulative_pnl += round_pnl
                            self._update_pnl_extremes()
                            self.total_rounds   += 1
                            if round_pnl > 0:
                                self.total_wins += 1
                            else:
                                self.total_losses += 1
                            self.csv.record(
                                crash_mult,
                                round_pnl=round_pnl,
                                total_win=self.cumulative_pnl,
                                highest_positive=self.highest_positive_pnl,
                                lowest_negative=self.lowest_negative_pnl,
                            )
                            log.info("ROUND %d | %s | round=%.2f KES | total=%.2f KES",
                                     self.total_rounds, desc, round_pnl, self.cumulative_pnl)
                            log.info("RUNNING BALANCE AFTER BET: %s", self._running_balance_text())

                            # ── P1 result ─────────────────────────────────────────────
                            if p1_this:
                                p1_session_pnl += p1_bet_used * (config.PANEL1_CASHOUT - 1) if crash_mult >= config.PANEL1_CASHOUT else -p1_bet_used
                                if crash_mult >= config.PANEL1_CASHOUT:
                                    if p1_was_assisting:
                                        p1_net_gain = round(p1_bet_used * (config.PANEL1_CASHOUT - 1), 2)
                                        old_p2_def = self.p2_recovery_deficit
                                        self.p2_recovery_deficit = max(0.0, round(self.p2_recovery_deficit - p1_net_gain, 2))
                                        log.info("P1 ASSIST WIN %.2fx — P2 deficit %.2f → %.2f KES.",
                                                 crash_mult, old_p2_def, self.p2_recovery_deficit)
                                        if self.p2_recovery_deficit <= 0:
                                            self._p2_step = 0
                                    elif config.RECOVERY_SCOPE == "percentage":
                                        total = self.recovery_deficit + self.p2_recovery_deficit
                                        max_steps = config.RECOVERY_STEPS if config.RECOVERY_STEPS > 0 else config.P1_MAX_BET_ROUNDS
                                        was_last  = (self._p1_step + 1) >= max_steps
                                        target = total if was_last else total * config.RECOVERY_PERCENTAGE / 100
                                        new_combined = round(max(0.0, total - target), 2)
                                        log.info("P1 WIN %.2fx — %s → %.2f KES deficit remaining.",
                                                 crash_mult,
                                                 "full recovery" if was_last else f"{config.RECOVERY_PERCENTAGE}% recovery",
                                                 new_combined)
                                        self.recovery_deficit    = new_combined
                                        self.p2_recovery_deficit = 0.0
                                    else:
                                        log.info("P1 WIN %.2fx — deficit cleared (was %.2f KES).",
                                                 crash_mult, self.recovery_deficit)
                                        self.recovery_deficit = 0.0
                                        if config.RECOVERY_SCOPE in ("combined", "smart"):
                                            self.p2_recovery_deficit = 0.0
                                    self._p1_consecutive_losses = 0
                                    p1_bet_plan    = []
                                    p1_session_pnl = 0.0
                                    self._p1_cooldown = config.BURST_COOLDOWN
                                    try:
                                        if self.p1_bet != config.BET_AMOUNT:
                                            await self._set_panel1_bet(frame, config.BET_AMOUNT)
                                            self.p1_bet = config.BET_AMOUNT
                                    except Exception:
                                        pass
                                else:
                                    if p1_was_assisting:
                                        self.recovery_deficit = round(self.recovery_deficit + p1_bet_used, 2)
                                        log.info("P1 ASSIST LOSS %.2fx — P1 takes %.2f KES debt → P1 deficit %.2f KES.",
                                                 crash_mult, p1_bet_used, self.recovery_deficit)
                                    elif config.RECOVERY_ENABLED:
                                        self.recovery_deficit = round(self.recovery_deficit + self.p1_bet, 2)
                                        log.info("P1 LOSS — deficit %.2f KES → next bet %.2f KES.",
                                                 self.recovery_deficit,
                                                 calc_p1_bet(self.recovery_deficit, self.p2_recovery_deficit, self._p1_step))
                                    self._p1_consecutive_losses += 1
                                    if (config.STOP_ON_CONSECUTIVE_LOSSES > 0
                                            and self._p1_consecutive_losses >= config.STOP_ON_CONSECUTIVE_LOSSES):
                                        log.warning("P1 consecutive loss limit (%d) — stopping.", self._p1_consecutive_losses)
                                        break
                                    if not p1_bet_plan:
                                        log.info("P1: pattern complete — back to WATCH. Deficit %.2f KES.",
                                                 self.recovery_deficit)
                                        p1_bet_plan    = []
                                        p1_session_pnl = 0.0
                                        self._p1_cooldown = config.BURST_COOLDOWN
                                        try:
                                            if self.p1_bet != config.BET_AMOUNT:
                                                await self._set_panel1_bet(frame, config.BET_AMOUNT)
                                                self.p1_bet = config.BET_AMOUNT
                                        except Exception:
                                            pass

                                if config.RECOVERY_SCOPE == "percentage" and config.RECOVERY_ENABLED:
                                    total_def = self.recovery_deficit + self.p2_recovery_deficit
                                    if total_def <= 0:
                                        self._p1_step = 0
                                    else:
                                        max_s = config.RECOVERY_STEPS if config.RECOVERY_STEPS > 0 else config.P1_MAX_BET_ROUNDS
                                        self._p1_step = 0 if (self._p1_step + 1) >= max_s else self._p1_step + 1

                            # ── P2 result ─────────────────────────────────────────────
                            if p2_this:
                                p2_session_pnl += p2_bet_used * (config.PANEL2_CASHOUT - 1) if crash_mult >= config.PANEL2_CASHOUT else -p2_bet_used
                                if crash_mult >= config.PANEL2_CASHOUT:
                                    if p2_was_assisting:
                                        p2_net_gain = round(p2_bet_used * (config.PANEL2_CASHOUT - 1), 2)
                                        old_p1_def = self.recovery_deficit
                                        self.recovery_deficit = max(0.0, round(self.recovery_deficit - p2_net_gain, 2))
                                        log.info("P2 ASSIST WIN %.2fx — P1 deficit %.2f → %.2f KES.",
                                                 crash_mult, old_p1_def, self.recovery_deficit)
                                    elif config.P2_RECOVERY_SCOPE == "percentage":
                                        max_steps = config.P2_RECOVERY_STEPS if config.P2_RECOVERY_STEPS > 0 else config.P2_MAX_BET_ROUNDS
                                        was_last  = (self._p2_step + 1) >= max_steps
                                        target = self.p2_recovery_deficit if was_last else self.p2_recovery_deficit * config.P2_RECOVERY_PERCENTAGE / 100
                                        remaining = round(max(0.0, self.p2_recovery_deficit - target), 2)
                                        log.info("P2 WIN %.2fx — %s → %.2f KES P2 deficit remaining.",
                                                 crash_mult,
                                                 "full recovery" if was_last else f"{config.P2_RECOVERY_PERCENTAGE}% recovery",
                                                 remaining)
                                        self.p2_recovery_deficit = remaining
                                    else:
                                        log.info("P2 WIN %.2fx — P2 deficit cleared (P1 deficit %.2f KES unchanged).",
                                                 crash_mult, self.recovery_deficit)
                                        self.p2_recovery_deficit = 0.0
                                    self._p2_consecutive_losses = 0
                                    p2_bet_plan    = []
                                    p2_session_pnl = 0.0
                                    self._p2_cooldown = config.BURST_COOLDOWN
                                    try:
                                        if self.p2_bet != config.P2_BET_AMOUNT:
                                            await self._set_panel2_bet(frame, config.P2_BET_AMOUNT)
                                            self.p2_bet = config.P2_BET_AMOUNT
                                    except Exception:
                                        pass
                                else:
                                    if p2_was_assisting:
                                        self.p2_recovery_deficit = round(self.p2_recovery_deficit + p2_bet_used, 2)
                                        log.info("P2 ASSIST LOSS %.2fx — P2 takes %.2f KES debt → P2 deficit %.2f KES.",
                                                 crash_mult, p2_bet_used, self.p2_recovery_deficit)
                                    elif config.P2_RECOVERY_ENABLED:
                                        self.p2_recovery_deficit = round(self.p2_recovery_deficit + self.p2_bet, 2)
                                        log.info("P2 LOSS — deficit %.2f KES → next bet %.2f KES.",
                                                 self.p2_recovery_deficit,
                                                 calc_p2_bet(self.recovery_deficit, self.p2_recovery_deficit, self._p2_step))
                                    self._p2_consecutive_losses += 1
                                    if (config.STOP_ON_CONSECUTIVE_LOSSES > 0
                                            and self._p2_consecutive_losses >= config.STOP_ON_CONSECUTIVE_LOSSES):
                                        log.warning("P2 consecutive loss limit (%d) — stopping.", self._p2_consecutive_losses)
                                        break
                                    if not p2_bet_plan:
                                        log.info("P2: pattern complete — back to WATCH. Deficit %.2f KES.",
                                                 self.p2_recovery_deficit)
                                        p2_bet_plan    = []
                                        p2_session_pnl = 0.0
                                        self._p2_cooldown = config.BURST_COOLDOWN
                                        try:
                                            if self.p2_bet != config.P2_BET_AMOUNT:
                                                await self._set_panel2_bet(frame, config.P2_BET_AMOUNT)
                                                self.p2_bet = config.P2_BET_AMOUNT
                                        except Exception:
                                            pass

                                if config.P2_RECOVERY_SCOPE == "percentage" and config.P2_RECOVERY_ENABLED:
                                    total_def = self.recovery_deficit + self.p2_recovery_deficit
                                    if total_def <= 0:
                                        self._p2_step = 0
                                    else:
                                        max_s = config.P2_RECOVERY_STEPS if config.P2_RECOVERY_STEPS > 0 else config.P2_MAX_BET_ROUNDS
                                        self._p2_step = 0 if (self._p2_step + 1) >= max_s else self._p2_step + 1

                        else:
                            self.csv.record(
                                crash_mult,
                                round_pnl=0.0,
                                total_win=self.cumulative_pnl,
                                highest_positive=self.highest_positive_pnl,
                                lowest_negative=self.lowest_negative_pnl,
                            )

                        # ── Check triggers for each panel independently ───────────────
                        if not p1_bet_plan:
                            if self._p1_cooldown > 0:
                                self._p1_cooldown -= 1
                                log.info("P1 cooldown: %d round(s) left.", self._p1_cooldown)
                            else:
                                _p1_mult_max = getattr(config, "P1_TRIGGER_MULT_MAX", float("inf"))
                                p1_trig_high = config.P1_TRIGGER_MULT < crash_mult <= _p1_mult_max
                                recent = history[:config.P1_LOW_STREAK_COUNT]
                                p1_trig_low = (len(recent) >= config.P1_LOW_STREAK_COUNT
                                               and all(m <= config.P1_LOW_STREAK_MAX for m in recent))
                                log.info("P1 WATCH | crash=%.2fx | high=%s | low=%s", crash_mult, p1_trig_high, p1_trig_low)
                                if p1_trig_high:
                                    p1_reason = f"crash {crash_mult:.2f}x in ({config.P1_TRIGGER_MULT:.1f}x, {_p1_mult_max:.1f}x]"
                                elif p1_trig_low:
                                    p1_reason = f"last {config.P1_LOW_STREAK_COUNT} crashes all ≤ {config.P1_LOW_STREAK_MAX:.1f}x"
                                else:
                                    p1_reason = None
                                if p1_reason:
                                    log.info("P1 TRIGGER (%s) — pattern %s", p1_reason, format_bet_pattern(p1_pattern))
                                    p1_bet_plan    = list(p1_pattern)
                                    p1_session_pnl = 0.0

                        if not p2_bet_plan:
                            if self._p2_cooldown > 0:
                                self._p2_cooldown -= 1
                                log.info("P2 cooldown: %d round(s) left.", self._p2_cooldown)
                            else:
                                _p2_mult_max = getattr(config, "P2_TRIGGER_MULT_MAX", float("inf"))
                                p2_trig_high = config.P2_TRIGGER_MULT < crash_mult <= _p2_mult_max
                                recent = history[:config.P2_LOW_STREAK_COUNT]
                                p2_trig_low = (len(recent) >= config.P2_LOW_STREAK_COUNT
                                               and all(m <= config.P2_LOW_STREAK_MAX for m in recent))
                                log.info("P2 WATCH | crash=%.2fx | high=%s | low=%s", crash_mult, p2_trig_high, p2_trig_low)
                                if p2_trig_high:
                                    p2_reason = f"crash {crash_mult:.2f}x in ({config.P2_TRIGGER_MULT:.1f}x, {_p2_mult_max:.1f}x]"
                                elif p2_trig_low:
                                    p2_reason = f"last {config.P2_LOW_STREAK_COUNT} crashes all ≤ {config.P2_LOW_STREAK_MAX:.1f}x"
                                else:
                                    p2_reason = None
                                if p2_reason:
                                    log.info("P2 TRIGGER (%s) — pattern %s", p2_reason, format_bet_pattern(p2_pattern))
                                    p2_bet_plan    = list(p2_pattern)
                                    p2_session_pnl = 0.0

                    break  # game loop exited normally — exit restart loop

                except KeyboardInterrupt:
                    raise  # bubble up to outer handler
                except Exception as e:
                    if not self.DEMO_MODE or _restarts >= _MAX_RESTARTS:
                        raise  # real money or too many retries — give up
                    _restarts += 1
                    log.warning(
                        "Session crashed (restart %d/%d): %s — reopening demo in 5 s…",
                        _restarts, _MAX_RESTARTS, e)
                    try:
                        await self.page.close()
                    except Exception:
                        pass
                    await asyncio.sleep(5)
                    # outer while True loops back → re-opens demo, deficits preserved

        except KeyboardInterrupt:
            log.info("Interrupted by user.")
        except Exception as e:
            log.exception("Unhandled error: %s", e)
        finally:
            self._print_summary()
            self.csv.close()
            await self.stop()

    def _print_summary(self):
        log.info("=" * 60)
        log.info("SESSION SUMMARY")
        log.info("  Rounds bet    : %d", self.total_rounds)
        log.info("  Wins          : %d", self.total_wins)
        log.info("  Losses        : %d", self.total_losses)
        rate = (self.total_wins / self.total_rounds * 100) if self.total_rounds else 0
        log.info("  Win rate      : %.1f%%", rate)
        log.info("  Net P&L       : KES %.2f", self.cumulative_pnl)
        log.info("  Highest +P&L  : KES %.2f", self.highest_positive_pnl)
        log.info("  Lowest -P&L   : KES %.2f", self.lowest_negative_pnl)
        log.info("=" * 60)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    await AviatorBot().run()


if __name__ == "__main__":
    asyncio.run(main())
