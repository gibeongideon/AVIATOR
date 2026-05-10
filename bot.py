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


def calc_p1_bet(p1_deficit: float, p2_deficit: float = 0.0, step: int = 0) -> float:
    if not config.RECOVERY_ENABLED:
        return config.BET_AMOUNT
    if config.RECOVERY_SCOPE == "individual":
        target = p1_deficit
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
    if config.P2_RECOVERY_SCOPE in ("individual", "smart"):
        target = p2_deficit   # P2 only covers its own; P1 is the big gun
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

    def __init__(self):
        os.makedirs("history", exist_ok=True)
        date_str = datetime.now().strftime("%Y%m%d")
        self.path = os.path.join("history", f"aviator_{date_str}.csv")
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
        p1_win = crash_mult >= config.PANEL1_CASHOUT
        p2_win = crash_mult >= config.PANEL2_CASHOUT
        self._csv.writerow({
            "timestamp":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "crash_mult":     f"{crash_mult:.2f}",
            "trigger":        1 if crash_mult > config.P1_TRIGGER_MULT else 0,
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

    def __init__(self):
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page:    Optional[Page]    = None

        # Totals
        self.total_rounds = 0
        self.total_wins   = 0
        self.total_losses = 0
        self.cumulative_pnl = 0.0

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

        self.csv = HistoryCSV()

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
        """Close SportPesa modals (Quick Deposit, cookie prompts, etc.)."""
        await asyncio.sleep(1.2)
        for sel in [
            '.modal.show .close',
            '.modal.show button[data-dismiss="modal"]',
            '.modal.show [aria-label="Close"]',
            'button[data-dismiss="modal"]',
            '.modal__close', '.dialog__close', '.popup__close',
            '[data-testid="modal-close-button"]',
            '[aria-label="Close"]', '[aria-label="close"]',
            '.quick-deposit .close', '.deposit-modal .close',
            'button.close:visible',
        ]:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    await asyncio.sleep(0.5)
                    log.info("Dismissed popup: %s", sel)
                    return
            except Exception:
                continue
        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass

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

    async def open_aviator(self):
        log.info("Opening Aviator…")
        await self.page.goto(config.AVIATOR_URL, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(1500)
        try:
            await self.page.click(SEL["cookie_accept"], timeout=4_000)
            log.info("Cookie banner dismissed.")
        except PWTimeout:
            pass
        await self._dismiss_page_popups()
        log.info("Waiting for Spribe game frame + inputs…")
        frame = await self._wait_for_frame(timeout_s=30)
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
        bet_inputs = await frame.query_selector_all('input[placeholder="1"]')
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
        bet_inputs = await frame.query_selector_all('input[placeholder="1"]')
        if bet_inputs:
            await set_input(bet_inputs[0], amount)
            log.info("P1 bet → %.2f KES (P1 deficit: %.2f KES).", amount, self.recovery_deficit)

    async def _set_panel2_bet(self, frame, amount: float):
        bet_inputs = await frame.query_selector_all('input[placeholder="1"]')
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
        try:
            await self.login()
            frame = await self.open_aviator()
            await self.setup_panels(frame)

            # ── Per-panel independent state ───────────────────────────────────
            p1_bet_next    = False
            p1_rounds_left = 0
            p1_session_pnl = 0.0

            p2_bet_next    = False
            p2_rounds_left = 0
            p2_session_pnl = 0.0

            history = await get_crash_history(frame)

            log.info("=" * 60)
            log.info("Strategy active — INDEPENDENT TRIGGERS")
            log.info("  P1: trigger > %.1fx | low ≤%.1fx × %d | max %d rounds | cashout %.1fx",
                     config.P1_TRIGGER_MULT, config.P1_LOW_STREAK_MAX,
                     config.P1_LOW_STREAK_COUNT, config.P1_MAX_BET_ROUNDS, config.PANEL1_CASHOUT)
            log.info("  P2: trigger > %.1fx | low ≤%.1fx × %d | max %d rounds | cashout %.1fx",
                     config.P2_TRIGGER_MULT, config.P2_LOW_STREAK_MAX,
                     config.P2_LOW_STREAK_COUNT, config.P2_MAX_BET_ROUNDS, config.PANEL2_CASHOUT)
            log.info("  Stop profit KES %.0f | Stop loss KES %.0f",
                     config.STOP_ON_PROFIT, config.STOP_ON_LOSS)
            log.info("=" * 60)

            while True:
                # Global guard
                reason = self.should_stop()
                if reason:
                    log.info("Bot stopping: %s", reason)
                    break

                # Always use a fresh frame reference — the iframe reloads periodically
                frame = self._get_frame()
                if frame is None:
                    log.warning("Game frame lost — waiting for it to reload…")
                    try:
                        frame = await self._wait_for_frame(timeout_s=30)
                        log.info("Frame recovered.")
                    except TimeoutError:
                        log.error("Frame never came back — aborting.")
                        break

                # Wait for the betting window to open
                log.info("Waiting for bet phase… [P1=%s P2=%s]",
                         "BET" if p1_bet_next else "watch",
                         "BET" if p2_bet_next else "watch")
                try:
                    ok = await wait_for_bet_phase(frame)
                except Exception as e:
                    log.warning("Frame context lost during bet-phase wait (%s) — retrying.", e)
                    continue
                if not ok:
                    log.error("Bet phase never opened — aborting.")
                    break

                # Snapshot which panels are betting this round
                p1_this = p1_bet_next
                p2_this = p2_bet_next

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
                    if p1_this: p1_rounds_left -= 1
                    if p2_this: p2_rounds_left -= 1
                    continue

                prev_history = await get_crash_history(frame)

                # ── Place bets for active panels ──────────────────────────────
                if p1_this or p2_this:
                    try:
                        placed = await self.place_bets(frame, p1=p1_this, p2=p2_this)
                    except Exception as e:
                        log.warning("Frame stale placing bet (%s) — skipping round.", e)
                        if p1_this: p1_rounds_left -= 1
                        if p2_this: p2_rounds_left -= 1
                        continue
                    if not placed:
                        log.warning("Could not place bets — skipping round.")
                        if p1_this: p1_rounds_left -= 1
                        if p2_this: p2_rounds_left -= 1
                        continue

                # ── Wait for round end ────────────────────────────────────────
                try:
                    history = await wait_for_round_end(frame, prev_history)
                except TimeoutError:
                    log.error("Round end timeout — resetting both panels to watch.")
                    p1_bet_next = p2_bet_next = False
                    continue
                except Exception as e:
                    log.warning("Frame stale waiting for round end (%s) — resetting.", e)
                    p1_bet_next = p2_bet_next = False
                    continue

                crash_mult = history[0]

                # ── Process results for betting panels ────────────────────────
                if p1_this or p2_this:
                    p1_bet_used = self.p1_bet if p1_this else 0.0
                    p2_bet_used = self.p2_bet if p2_this else 0.0
                    round_pnl, desc = calc_round_pnl(crash_mult, p1_bet_used, p2_bet_used)
                    self.cumulative_pnl += round_pnl
                    self.total_rounds   += 1
                    if round_pnl > 0:
                        self.total_wins += 1
                    else:
                        self.total_losses += 1
                    self.csv.record(
                        crash_mult, mode="bet",
                        round_pnl=round_pnl,
                        session_pnl=p1_session_pnl + p2_session_pnl,
                        cumulative_pnl=self.cumulative_pnl,
                    )
                    log.info("ROUND %d | %s | round=%.2f KES | total=%.2f KES",
                             self.total_rounds, desc, round_pnl, self.cumulative_pnl)

                    # ── P1 result ─────────────────────────────────────────────
                    if p1_this:
                        p1_rounds_left -= 1
                        p1_session_pnl += p1_bet_used * (config.PANEL1_CASHOUT - 1) if crash_mult >= config.PANEL1_CASHOUT else -p1_bet_used
                        if crash_mult >= config.PANEL1_CASHOUT:
                            if config.RECOVERY_SCOPE == "percentage":
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
                            p1_bet_next    = False
                            p1_session_pnl = 0.0
                            self._p1_cooldown = config.BURST_COOLDOWN
                            try:
                                if self.p1_bet != config.BET_AMOUNT:
                                    await self._set_panel1_bet(frame, config.BET_AMOUNT)
                                    self.p1_bet = config.BET_AMOUNT
                            except Exception:
                                pass
                        else:
                            if config.RECOVERY_ENABLED:
                                self.recovery_deficit = round(self.recovery_deficit + self.p1_bet, 2)
                                log.info("P1 LOSS — deficit %.2f KES → next bet %.2f KES.",
                                         self.recovery_deficit,
                                         calc_p1_bet(self.recovery_deficit, self.p2_recovery_deficit, self._p1_step))
                            self._p1_consecutive_losses += 1
                            if (config.STOP_ON_CONSECUTIVE_LOSSES > 0
                                    and self._p1_consecutive_losses >= config.STOP_ON_CONSECUTIVE_LOSSES):
                                log.warning("P1 consecutive loss limit (%d) — stopping.", self._p1_consecutive_losses)
                                break
                            if p1_rounds_left <= 0:
                                log.info("P1: all %d rounds used — back to WATCH. Deficit %.2f KES.",
                                         config.P1_MAX_BET_ROUNDS, self.recovery_deficit)
                                p1_bet_next    = False
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
                        p2_rounds_left -= 1
                        p2_session_pnl += p2_bet_used * (config.PANEL2_CASHOUT - 1) if crash_mult >= config.PANEL2_CASHOUT else -p2_bet_used
                        if crash_mult >= config.PANEL2_CASHOUT:
                            if config.P2_RECOVERY_SCOPE == "percentage":
                                max_steps = config.P2_RECOVERY_STEPS if config.P2_RECOVERY_STEPS > 0 else config.P2_MAX_BET_ROUNDS
                                was_last  = (self._p2_step + 1) >= max_steps
                                target = self.p2_recovery_deficit if was_last else self.p2_recovery_deficit * config.P2_RECOVERY_PERCENTAGE / 100
                                remaining = round(max(0.0, self.p2_recovery_deficit - target), 2)
                                log.info("P2 WIN %.2fx — %s → %.2f KES P2 deficit remaining.",
                                         crash_mult,
                                         "full recovery" if was_last else f"{config.P2_RECOVERY_PERCENTAGE}% recovery",
                                         remaining)
                                self.p2_recovery_deficit = remaining
                                # P1 deficit unchanged — P2 win never covers P1 losses
                            else:
                                # "individual", "combined", "smart": P2 win clears only P2 deficit
                                # P1 deficit unchanged — only a P1 WIN covers P1 losses
                                log.info("P2 WIN %.2fx — P2 deficit cleared (P1 deficit %.2f KES unchanged).",
                                         crash_mult, self.recovery_deficit)
                                self.p2_recovery_deficit = 0.0
                            self._p2_consecutive_losses = 0
                            p2_bet_next    = False
                            p2_session_pnl = 0.0
                            self._p2_cooldown = config.BURST_COOLDOWN
                            try:
                                if self.p2_bet != config.P2_BET_AMOUNT:
                                    await self._set_panel2_bet(frame, config.P2_BET_AMOUNT)
                                    self.p2_bet = config.P2_BET_AMOUNT
                            except Exception:
                                pass
                        else:
                            if config.P2_RECOVERY_ENABLED:
                                self.p2_recovery_deficit = round(self.p2_recovery_deficit + self.p2_bet, 2)
                                log.info("P2 LOSS — deficit %.2f KES → next bet %.2f KES.",
                                         self.p2_recovery_deficit,
                                         calc_p2_bet(self.recovery_deficit, self.p2_recovery_deficit, self._p2_step))
                            self._p2_consecutive_losses += 1
                            if (config.STOP_ON_CONSECUTIVE_LOSSES > 0
                                    and self._p2_consecutive_losses >= config.STOP_ON_CONSECUTIVE_LOSSES):
                                log.warning("P2 consecutive loss limit (%d) — stopping.", self._p2_consecutive_losses)
                                break
                            if p2_rounds_left <= 0:
                                log.info("P2: all %d rounds used — back to WATCH. Deficit %.2f KES.",
                                         config.P2_MAX_BET_ROUNDS, self.p2_recovery_deficit)
                                p2_bet_next    = False
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
                    self.csv.record(crash_mult, mode="watch", cumulative_pnl=self.cumulative_pnl)

                # ── Check triggers for each panel independently ───────────────
                if not p1_bet_next:
                    if self._p1_cooldown > 0:
                        self._p1_cooldown -= 1
                        log.info("P1 cooldown: %d round(s) left.", self._p1_cooldown)
                    else:
                        p1_trig_high = crash_mult > config.P1_TRIGGER_MULT
                        recent = history[:config.P1_LOW_STREAK_COUNT]
                        p1_trig_low = (len(recent) >= config.P1_LOW_STREAK_COUNT
                                       and all(m <= config.P1_LOW_STREAK_MAX for m in recent))
                        log.info("P1 WATCH | crash=%.2fx | high=%s | low=%s", crash_mult, p1_trig_high, p1_trig_low)
                        if p1_trig_high:
                            p1_reason = f"crash {crash_mult:.2f}x > {config.P1_TRIGGER_MULT:.1f}x"
                        elif p1_trig_low:
                            p1_reason = f"last {config.P1_LOW_STREAK_COUNT} crashes all ≤ {config.P1_LOW_STREAK_MAX:.1f}x"
                        else:
                            p1_reason = None
                        if p1_reason:
                            log.info("P1 TRIGGER (%s) — betting next %d round(s)!", p1_reason, config.P1_MAX_BET_ROUNDS)
                            p1_bet_next    = True
                            p1_rounds_left = config.P1_MAX_BET_ROUNDS
                            p1_session_pnl = 0.0

                if not p2_bet_next:
                    if self._p2_cooldown > 0:
                        self._p2_cooldown -= 1
                        log.info("P2 cooldown: %d round(s) left.", self._p2_cooldown)
                    else:
                        p2_trig_high = crash_mult > config.P2_TRIGGER_MULT
                        recent = history[:config.P2_LOW_STREAK_COUNT]
                        p2_trig_low = (len(recent) >= config.P2_LOW_STREAK_COUNT
                                       and all(m <= config.P2_LOW_STREAK_MAX for m in recent))
                        log.info("P2 WATCH | crash=%.2fx | high=%s | low=%s", crash_mult, p2_trig_high, p2_trig_low)
                        if p2_trig_high:
                            p2_reason = f"crash {crash_mult:.2f}x > {config.P2_TRIGGER_MULT:.1f}x"
                        elif p2_trig_low:
                            p2_reason = f"last {config.P2_LOW_STREAK_COUNT} crashes all ≤ {config.P2_LOW_STREAK_MAX:.1f}x"
                        else:
                            p2_reason = None
                        if p2_reason:
                            log.info("P2 TRIGGER (%s) — betting next %d round(s)!", p2_reason, config.P2_MAX_BET_ROUNDS)
                            p2_bet_next    = True
                            p2_rounds_left = config.P2_MAX_BET_ROUNDS
                            p2_session_pnl = 0.0

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
        log.info("=" * 60)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    await AviatorBot().run()


if __name__ == "__main__":
    asyncio.run(main())
