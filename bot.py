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
    # Login page
    "login_user":     'input[name="username"], input[type="tel"], input[placeholder*="phone" i]',
    "login_pass":     'input[name="password"], input[type="password"]',
    "login_btn":      'button[type="submit"], button:has-text("Log in"), button:has-text("Login")',

    # Aviator iframe (the game lives inside an <iframe>)
    "iframe":         'iframe[src*="aviator"], iframe[id*="aviator"], iframe[title*="aviator" i]',

    # Inside the Aviator iframe
    "bet_input":      '.bet-input input, input[class*="bet"], [data-testid="bet-amount"]',
    "bet_btn":        'button.bet-btn, button[class*="bet"]:not([class*="cashout"]), [data-testid="place-bet"]',
    "cashout_btn":    'button.cashout-btn, button[class*="cashout"], [data-testid="cashout"]',
    "multiplier":     '.multiplier, [class*="multiplier"], [class*="coefficient"]',
    "balance":        '[class*="balance"], [data-testid="balance"]',
    "game_status":    '[class*="game-status"], [class*="status"]',
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

    async def open_aviator(self):
        log.info("Opening Aviator game…")
        await self.page.goto(config.AVIATOR_URL, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(3000)

        # Some casino games load inside an iframe
        iframe_el = await self.page.query_selector(SEL["iframe"])
        if iframe_el:
            log.info("Game iframe detected — switching context.")
            frame = await iframe_el.content_frame()
            return frame
        else:
            log.info("No iframe detected — using main page context.")
            return self.page

    # ── Single round ──────────────────────────────────────────────────────────

    async def play_round(self, frame) -> float:
        """
        Place one bet and cash out at the configured multiplier.
        Returns net PnL for this round (positive = win, negative = loss).
        """
        # Set bet amount
        try:
            await frame.fill(SEL["bet_input"], str(config.BET_AMOUNT))
        except Exception as e:
            log.warning("Could not set bet amount: %s", e)

        # Click BET
        log.info("Placing bet of KES %s …", config.BET_AMOUNT)
        clicked = await safe_click(frame, SEL["bet_btn"])
        if not clicked:
            log.error("Could not click BET button — skipping round.")
            return 0.0

        await frame.wait_for_timeout(500)

        # Wait for cashout target OR game crash
        try:
            reached = await wait_for_multiplier_above(frame, config.AUTO_CASHOUT_AT)
            log.info("Multiplier hit %.2fx — cashing out!", reached)
            await safe_click(frame, SEL["cashout_btn"])
            profit = config.BET_AMOUNT * (reached - 1)
            return profit
        except TimeoutError:
            # Plane crashed before we could cash out (or we missed it)
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
