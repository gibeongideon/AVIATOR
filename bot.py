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
import logging
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
    # Game frame — inputs in order: [0] P1 bet, [1] P1 cashout, [2] P2 bet, [3] P2 cashout
    "all_inputs":   'input',
    # Green BET button (both panels)
    "bet_btn":      'button.btn-success.bet',
    # Auto tab (switches panel to auto-cashout mode)
    "auto_tab":     'button.tab',
    # Crash history bar (newest crash is first line)
    "history":      'div.result-history',
}


# ── Low-level helpers ─────────────────────────────────────────────────────────

async def set_input(inp, value):
    """Reliably set an Angular input: fill + Tab to commit the value."""
    await inp.click()
    await inp.press("Control+a")
    await inp.fill(str(value))
    await inp.press("Tab")              # triggers Angular's (blur) / (change) binding


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


def calc_round_pnl(crash_mult: float) -> tuple[float, str]:
    """
    Return (net_pnl, description) for a round given the crash multiplier.
    Panel 1 cashes out at PANEL1_CASHOUT, Panel 2 at PANEL2_CASHOUT.
    """
    bet = config.BET_AMOUNT
    p1_win = crash_mult >= config.PANEL1_CASHOUT
    p2_win = crash_mult >= config.PANEL2_CASHOUT

    pnl = 0.0
    pnl += bet * (config.PANEL1_CASHOUT - 1) if p1_win else -bet
    pnl += bet * (config.PANEL2_CASHOUT - 1) if p2_win else -bet

    desc = (
        f"P1={'WIN @{:.0f}x'.format(config.PANEL1_CASHOUT) if p1_win else 'LOSS'}"
        f"  P2={'WIN @{:.0f}x'.format(config.PANEL2_CASHOUT) if p2_win else 'LOSS'}"
        f"  crash={crash_mult:.2f}x"
    )
    return pnl, desc


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
        except PWTimeout:
            log.error("Login may have failed — still on login page.")
            raise

    # ── Open game ─────────────────────────────────────────────────────────────

    async def _find_spribe_frame(self, timeout_s=25):
        for _ in range(timeout_s * 2):
            for f in self.page.frames:
                if "spribegaming.com" in f.url or "aviator-next" in f.url:
                    return f
            await asyncio.sleep(0.5)
        raise TimeoutError("Spribe game frame not found after %ds" % timeout_s)

    async def open_aviator(self):
        log.info("Opening Aviator…")
        await self.page.goto(config.AVIATOR_URL, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(1500)
        try:
            await self.page.click(SEL["cookie_accept"], timeout=4_000)
            log.info("Cookie banner dismissed.")
        except PWTimeout:
            pass
        frame = await self._find_spribe_frame()
        log.info("Game frame: %s", frame.url[:70])
        await self.page.wait_for_timeout(4000)
        return frame

    # ── One-time panel setup ──────────────────────────────────────────────────

    async def setup_panels(self, frame):
        """
        Switch both panels to Auto mode, set cashout odds and bet amounts.
        This runs once at startup; values persist between rounds.
        """
        log.info("Setting up panels (Auto cashout + bet amounts)…")

        # Click Auto tab on any panel that isn't already in Auto mode
        tabs = await frame.query_selector_all(SEL["auto_tab"])
        for tab in tabs:
            txt = (await tab.inner_text()).strip()
            cls = await tab.get_attribute("class") or ""
            if txt == "Auto" and "active" not in cls:
                await tab.click()
                await asyncio.sleep(0.3)

        await asyncio.sleep(0.5)

        # inputs: [0] P1-bet, [1] P1-cashout, [2] P2-bet, [3] P2-cashout
        inputs = await frame.query_selector_all(SEL["all_inputs"])
        if len(inputs) < 4:
            log.warning("Expected 4 inputs, found %d — setup may be incomplete.", len(inputs))
        else:
            await set_input(inputs[0], config.BET_AMOUNT)       # Panel 1 bet: 1 KES
            await set_input(inputs[1], config.PANEL1_CASHOUT)   # Panel 1 cashout: 6x
            await set_input(inputs[2], config.BET_AMOUNT)       # Panel 2 bet: 1 KES
            await set_input(inputs[3], config.PANEL2_CASHOUT)   # Panel 2 cashout: 3x

            # Read back to confirm values stuck
            v0 = await inputs[0].input_value()
            v1 = await inputs[1].input_value()
            v2 = await inputs[2].input_value()
            v3 = await inputs[3].input_value()
            log.info("Panel setup confirmed — P1 bet=%s cashout=%s | P2 bet=%s cashout=%s", v0, v1, v2, v3)

    # ── Place bets on both panels ─────────────────────────────────────────────

    async def place_bets(self, frame) -> bool:
        btns = await frame.query_selector_all(SEL["bet_btn"])
        if not btns:
            log.warning("BET buttons not found — bet phase may have already closed.")
            return False
        await btns[0].click()
        if len(btns) > 1:
            await asyncio.sleep(0.1)
            await btns[1].click()
        log.info("Bets placed on %d panel(s).", min(len(btns), 2))
        return True

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

            # State
            watching     = True    # True = watching for trigger
            bet_next     = False   # True = place bets when bet phase opens
            rounds_left  = 0       # rounds remaining in current betting burst
            session_pnl  = 0.0    # P&L since last trigger
            history      = await get_crash_history(frame)

            log.info("=" * 60)
            log.info("Strategy active")
            log.info("  Trigger : last crash > %.1fx", config.TRIGGER_MULT)
            log.info("  Max rounds per burst : %d", config.MAX_BET_ROUNDS)
            log.info("  Panel 1 : KES %.0f  auto-cashout @ %.1fx", config.BET_AMOUNT, config.PANEL1_CASHOUT)
            log.info("  Panel 2 : KES %.0f  auto-cashout @ %.1fx", config.BET_AMOUNT, config.PANEL2_CASHOUT)
            log.info("  Stop profit : KES %.0f  |  Stop loss : KES %.0f", config.STOP_ON_PROFIT, config.STOP_ON_LOSS)
            log.info("=" * 60)

            while True:
                # Global guard
                reason = self.should_stop()
                if reason:
                    log.info("Bot stopping: %s", reason)
                    break

                # Wait for the betting window to open
                log.info("Waiting for bet phase…")
                ok = await wait_for_bet_phase(frame)
                if not ok:
                    log.error("Bet phase never opened — aborting.")
                    break

                if bet_next:
                    # ── Betting round ─────────────────────────────────────────
                    prev_history = await get_crash_history(frame)
                    placed = await self.place_bets(frame)

                    if not placed:
                        log.warning("Could not place bets — skipping round.")
                        rounds_left -= 1
                    else:
                        # Wait for round to finish
                        try:
                            history = await wait_for_round_end(frame, prev_history)
                        except TimeoutError:
                            log.error("Round end timeout — resetting to watch mode.")
                            watching, bet_next = True, False
                            continue

                        crash_mult = history[0]
                        round_pnl, desc = calc_round_pnl(crash_mult)
                        session_pnl     += round_pnl
                        self.cumulative_pnl += round_pnl
                        self.total_rounds   += 1
                        rounds_left         -= 1

                        if round_pnl > 0:
                            self.total_wins += 1
                        else:
                            self.total_losses += 1

                        log.info(
                            "ROUND %d | %s | round=%.2f KES | session=%.2f KES | total=%.2f KES",
                            self.total_rounds, desc, round_pnl, session_pnl, self.cumulative_pnl,
                        )

                    # ── Decide what to do next ────────────────────────────────
                    if session_pnl > 0:
                        log.info(
                            "Recovered! Session P&L = +%.2f KES — returning to WATCH mode.",
                            session_pnl,
                        )
                        bet_next, watching = False, True
                        session_pnl = 0.0

                    elif rounds_left <= 0:
                        log.info(
                            "Max rounds reached. Session P&L = %.2f KES — taking the loss, WATCH mode.",
                            session_pnl,
                        )
                        bet_next, watching = False, True
                        session_pnl = 0.0

                    else:
                        log.info("%d round(s) left in burst. Betting again next round.", rounds_left)
                        # bet_next stays True

                else:
                    # ── Watch round (no bet) ──────────────────────────────────
                    prev_history = await get_crash_history(frame)
                    try:
                        history = await wait_for_round_end(frame, prev_history)
                    except TimeoutError:
                        log.warning("Round end timeout during watch — retrying.")
                        continue

                    crash_mult = history[0]
                    log.info("WATCH | crash=%.2fx | trigger>%.1fx", crash_mult, config.TRIGGER_MULT)

                    if crash_mult > config.TRIGGER_MULT:
                        log.info(
                            "TRIGGER HIT (%.2fx > %.1fx) — betting next %d round(s)!",
                            crash_mult, config.TRIGGER_MULT, config.MAX_BET_ROUNDS,
                        )
                        bet_next     = True
                        rounds_left  = config.MAX_BET_ROUNDS
                        session_pnl  = 0.0

        except KeyboardInterrupt:
            log.info("Interrupted by user.")
        except Exception as e:
            log.exception("Unhandled error: %s", e)
        finally:
            self._print_summary()
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
