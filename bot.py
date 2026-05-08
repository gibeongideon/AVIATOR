"""
SportPesa Aviator Bot — Playwright (Python)
Run: python bot.py
"""

import asyncio
import logging
import sys
from datetime import datetime
from typing import Optional

from playwright.async_api import async_playwright, Page, Browser, BrowserContext, TimeoutError as PWTimeout

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


# ── Selectors ─────────────────────────────────────────────────────────────────
# These may change if SportPesa updates their frontend — inspect with DevTools if broken.
SEL = {
    # Login page  (confirmed via inspector.py 2026-05-09)
    "login_user":     'input[name="user"]',
    "login_pass":     'input[name="password"]',
    "login_btn":      '[data-testid="login-form-submit-button"]',

    # Cookie consent banner on the main SportPesa page
    "cookie_accept":  'button.btn-primary',

    # ── Inside the Spribe Aviator iframe (confirmed 2026-05-09) ───────────────
    # Bet amount input (first panel)
    "bet_input":      'input[placeholder="1"]',

    # Green BET button (waiting state) → class="btn btn-success bet ng-star-inserted"
    "bet_btn":        'button.btn-success.bet',

    # CASH OUT button (appears during flight, replaces BET button)
    # Spribe changes btn-success → btn-warning when cashing out is possible
    "cashout_btn":    'button.btn-warning.bet, button.cashout',

    # Multiplier shown during the flying phase (above the plane)
    # Spribe renders it in a <div class="bubble"> or <span class="coefficient">
    "multiplier":     '.bubble span, .coefficient, [class*="multiplier-value"], .sky-info span',
}


# ── Helpers ───────────────────────────────────────────────────────────────────

async def safe_click(page: Page, selector: str, timeout: int = 10_000) -> bool:
    try:
        await page.wait_for_selector(selector, timeout=timeout)
        await page.click(selector)
        return True
    except PWTimeout:
        log.warning("Click timeout: %s", selector)
        return False


async def get_text(page: Page, selector: str) -> Optional[str]:
    try:
        el = await page.query_selector(selector)
        if el:
            return (await el.inner_text()).strip()
    except Exception:
        pass
    return None


async def get_multiplier(frame) -> Optional[float]:
    """Extract the current multiplier value from the game frame."""
    try:
        el = await frame.query_selector(SEL["multiplier"])
        if el:
            raw = (await el.inner_text()).strip().replace("x", "").replace(",", ".")
            return float(raw)
    except Exception:
        pass
    return None


async def wait_for_multiplier_above(frame, target: float, poll_ms: int = 200, timeout_s: int = 120) -> float:
    """Poll until the multiplier exceeds target, then return its value."""
    elapsed = 0
    while elapsed < timeout_s * 1000:
        val = await get_multiplier(frame)
        if val is not None and val >= target:
            return val
        await asyncio.sleep(poll_ms / 1000)
        elapsed += poll_ms
    raise TimeoutError(f"Multiplier never reached {target}x within {timeout_s}s")


# ── Core bot ──────────────────────────────────────────────────────────────────

class AviatorBot:
    def __init__(self):
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

        self.rounds_played  = 0
        self.wins           = 0
        self.losses         = 0
        self.loss_streak    = 0
        self.cumulative_pnl = 0.0   # profit / loss in KES

    # ── Browser setup ─────────────────────────────────────────────────────────

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
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        self.context.set_default_timeout(config.BROWSER_TIMEOUT)
        self.page = await self.context.new_page()
        log.info("Browser started.")

    async def stop(self):
        if self.browser:
            await self.browser.close()
        log.info("Browser closed.")

    # ── Login ─────────────────────────────────────────────────────────────────

    async def login(self):
        log.info("Navigating to login page…")
        await self.page.goto(config.LOGIN_URL, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(2000)

        log.info("Entering credentials…")
        await self.page.fill(SEL["login_user"], config.USERNAME)
        await self.page.fill(SEL["login_pass"], config.PASSWORD)
        await safe_click(self.page, SEL["login_btn"])

        # Wait for redirect away from login page
        try:
            await self.page.wait_for_url(lambda url: "login" not in url, timeout=15_000)
            log.info("Login successful.")
        except PWTimeout:
            log.error("Login may have failed — still on login page. Check credentials.")
            raise

    # ── Navigate to Aviator ───────────────────────────────────────────────────

    async def _find_spribe_frame(self, timeout_s: int = 20):
        """Poll page.frames until the spribegaming.com frame appears."""
        for _ in range(timeout_s * 2):
            for f in self.page.frames:
                if "spribegaming.com" in f.url or "aviator-next" in f.url:
                    return f
            await asyncio.sleep(0.5)
        raise TimeoutError("Spribe Aviator game frame not found after %ds" % timeout_s)

    async def open_aviator(self):
        log.info("Opening Aviator game…")
        await self.page.goto(config.AVIATOR_URL, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(2000)

        # Dismiss cookie/consent banner if present
        try:
            await self.page.click(SEL["cookie_accept"], timeout=4_000)
            log.info("Cookie banner dismissed.")
            await self.page.wait_for_timeout(1000)
        except PWTimeout:
            pass

        # Poll page.frames — CSS wait_for_selector doesn't work for JS-injected iframes
        log.info("Waiting for Spribe Aviator game frame…")
        frame = await self._find_spribe_frame(timeout_s=20)
        log.info("Game frame found: %s", frame.url[:70])

        # Give the Spribe game JS time to initialise controls
        await self.page.wait_for_timeout(4000)
        return frame

    # ── Single round ──────────────────────────────────────────────────────────

    async def play_round(self, frame) -> float:
        """
        Place one bet and cash out at the configured multiplier.
        Returns net PnL for this round (positive = win, negative = loss).
        """
        # Fill bet amount into the first panel's input (nth=0)
        try:
            inputs = await frame.query_selector_all(SEL["bet_input"])
            if inputs:
                await inputs[0].triple_click()  # select-all then overwrite
                await inputs[0].type(str(config.BET_AMOUNT))
            else:
                log.warning("Bet input not found — using default amount shown.")
        except Exception as e:
            log.warning("Could not set bet amount: %s", e)

        # Click the first BET button (panel 1)
        log.info("Placing bet of KES %s …", config.BET_AMOUNT)
        try:
            btns = await frame.query_selector_all(SEL["bet_btn"])
            if not btns:
                log.error("BET button not found — skipping round.")
                return 0.0
            await btns[0].click()
        except Exception as e:
            log.error("Could not click BET button: %s — skipping round.", e)
            return 0.0

        await frame.wait_for_timeout(500)

        # Wait for cashout target OR game crash
        try:
            reached = await wait_for_multiplier_above(frame, config.AUTO_CASHOUT_AT)
            log.info("Multiplier hit %.2fx — cashing out!", reached)
            # Cash out button appears in place of BET during flight
            cashout_btns = await frame.query_selector_all(SEL["cashout_btn"])
            if cashout_btns:
                await cashout_btns[0].click()
            profit = config.BET_AMOUNT * (reached - 1)
            return profit
        except TimeoutError:
            log.info("Crashed before %.2fx — round lost.", config.AUTO_CASHOUT_AT)
            return -config.BET_AMOUNT

    # ── Stop conditions ───────────────────────────────────────────────────────

    def should_stop(self) -> Optional[str]:
        if config.MAX_ROUNDS and self.rounds_played >= config.MAX_ROUNDS:
            return f"Reached max rounds ({config.MAX_ROUNDS})"
        if config.STOP_ON_LOSS_STREAK and self.loss_streak >= config.STOP_ON_LOSS_STREAK:
            return f"Loss streak of {self.loss_streak} reached"
        if config.STOP_ON_PROFIT and self.cumulative_pnl >= config.STOP_ON_PROFIT:
            return f"Profit target reached (KES {self.cumulative_pnl:.2f})"
        if config.STOP_ON_LOSS and self.cumulative_pnl <= config.STOP_ON_LOSS:
            return f"Loss limit reached (KES {self.cumulative_pnl:.2f})"
        return None

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self):
        await self.start()
        try:
            await self.login()
            frame = await self.open_aviator()

            log.info("=" * 55)
            log.info("Bot running — cash-out target: %.2fx | bet: KES %s",
                     config.AUTO_CASHOUT_AT, config.BET_AMOUNT)
            log.info("=" * 55)

            while True:
                reason = self.should_stop()
                if reason:
                    log.info("Stopping: %s", reason)
                    break

                self.rounds_played += 1
                log.info("─── Round %d ───", self.rounds_played)

                pnl = await self.play_round(frame)
                self.cumulative_pnl += pnl

                if pnl > 0:
                    self.wins += 1
                    self.loss_streak = 0
                    log.info("WIN  +KES %.2f  |  total P&L: KES %.2f", pnl, self.cumulative_pnl)
                else:
                    self.losses += 1
                    self.loss_streak += 1
                    log.info("LOSS  KES %.2f  |  total P&L: KES %.2f", pnl, self.cumulative_pnl)

                # Brief pause between rounds
                await asyncio.sleep(2)

        except KeyboardInterrupt:
            log.info("Interrupted by user.")
        except Exception as e:
            log.exception("Unhandled error: %s", e)
        finally:
            self._print_summary()
            await self.stop()

    def _print_summary(self):
        log.info("=" * 55)
        log.info("SESSION SUMMARY")
        log.info("  Rounds played : %d", self.rounds_played)
        log.info("  Wins          : %d", self.wins)
        log.info("  Losses        : %d", self.losses)
        win_rate = (self.wins / self.rounds_played * 100) if self.rounds_played else 0
        log.info("  Win rate      : %.1f%%", win_rate)
        log.info("  Net P&L       : KES %.2f", self.cumulative_pnl)
        log.info("=" * 55)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    bot = AviatorBot()
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
