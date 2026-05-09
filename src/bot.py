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
            return {"ok": True, "message": "Login successful — credentials are valid."}
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

    def __init__(self, session_id: str = "local"):
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
        p1_win = crash_mult >= config.PANEL1_CASHOUT
        p2_win = crash_mult >= config.PANEL2_CASHOUT
        self._csv.writerow({
            "timestamp":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "crash_mult":     f"{crash_mult:.2f}",
            "trigger":        1 if crash_mult > config.TRIGGER_MULT else 0,
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
    ):
        self._username   = username   or config.USERNAME
        self._password   = password   or config.PASSWORD
        self._headless   = headless   if headless is not None else config.HEADLESS
        self._session_id = session_id or "local"

        self._stop_event = asyncio.Event()

        # Per-session logger so multiple sessions don't collide
        self.log = logging.getLogger(f"aviator-bot.{self._session_id}")

        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page:    Optional[Page]    = None

        self.total_rounds = 0
        self.total_wins   = 0
        self.total_losses = 0
        self.cumulative_pnl = 0.0

        self.recovery_deficit = 0.0
        self.p1_bet = 1.0
        self.last_event = "idle"
        self.account_balance = "—"

        self.csv = HistoryCSV(session_id=self._session_id)

    def request_stop(self):
        self._stop_event.set()

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
            await self._read_balance()
        except PWTimeout:
            self.last_event = "Login failed — check credentials"
            self.log.error("Login may have failed — still on login page.")
            raise

    # ── Account balance ───────────────────────────────────────────────────────

    async def _read_balance(self):
        """Read account balance from the SportPesa header (main page, not iframe)."""
        if not self.page:
            return
        try:
            # Give Angular a moment to render the balance after navigation
            await asyncio.sleep(1.5)

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
        """Poll until the Spribe frame is present and has inputs loaded."""
        for _ in range(timeout_s * 2):
            frame = self._get_frame()
            if frame:
                try:
                    inputs = await frame.query_selector_all('input')
                    if inputs:
                        return frame
                except Exception:
                    pass   # frame found but context not ready yet
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
        self.last_event = "Waiting for game to load…"
        self.log.info("Waiting for Spribe game frame + inputs…")
        frame = await self._wait_for_frame(timeout_s=30)
        self.last_event = "Game loaded — setting up panels"
        self.log.info("Game ready: %s", frame.url[:70])
        await self.page.wait_for_timeout(1000)
        return frame

    # ── One-time panel setup ──────────────────────────────────────────────────

    async def _setup_one_panel(self, frame, panel_idx: int, cashout_target: float):
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
        bet_inputs = await frame.query_selector_all('input[placeholder="1"]')
        if panel_idx < len(bet_inputs):
            await set_input(bet_inputs[panel_idx], config.BET_AMOUNT)
            self.log.info("  Panel %d: bet amount set to %s KES.", panel_idx, config.BET_AMOUNT)

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
                 config.PANEL1_CASHOUT, config.BET_AMOUNT)
        await self._setup_one_panel(frame, panel_idx=0, cashout_target=config.PANEL1_CASHOUT)

        self.last_event = "Setting up Panel 2…"
        self.log.info("Setting up Panel 2 (cashout=%.1fx, bet=%s KES)…",
                 config.PANEL2_CASHOUT, config.BET_AMOUNT)
        await self._setup_one_panel(frame, panel_idx=1, cashout_target=config.PANEL2_CASHOUT)

        # ── Verify all visible inputs ─────────────────────────────────────────
        await asyncio.sleep(0.4)
        visible_vals = []
        for inp in await frame.query_selector_all('input'):
            if await inp.is_visible():
                visible_vals.append(await inp.input_value())
        self.log.info("Visible input values after setup: %s", visible_vals)
        self.log.info("Setup complete — P1 bet=1 @%.1fx | P2 bet=1 @%.1fx",
                 config.PANEL1_CASHOUT, config.PANEL2_CASHOUT)

    # ── Panel 1 martingale bet update ─────────────────────────────────────────

    async def _set_panel1_bet(self, frame, amount: float):
        """Update only Panel 1's bet amount input in the UI."""
        bet_inputs = await frame.query_selector_all('input[placeholder="1"]')
        if bet_inputs:
            await set_input(bet_inputs[0], amount)
            self.log.info("P1 bet → %.2f KES (recovery deficit: %.2f KES).", amount, self.recovery_deficit)

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

            self.last_event = "Strategy active — watching for trigger"
            self.log.info("=" * 60)
            self.log.info("Strategy active")
            self.log.info("  Trigger : last crash > %.1fx", config.TRIGGER_MULT)
            self.log.info("  Max rounds per burst : %d", config.MAX_BET_ROUNDS)
            self.log.info("  Panel 1 : KES %.0f  auto-cashout @ %.1fx", config.BET_AMOUNT, config.PANEL1_CASHOUT)
            self.log.info("  Panel 2 : KES %.0f  auto-cashout @ %.1fx", config.BET_AMOUNT, config.PANEL2_CASHOUT)
            self.log.info("  Stop profit : KES %.0f  |  Stop loss : KES %.0f", config.STOP_ON_PROFIT, config.STOP_ON_LOSS)
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
                        self.p1_bet = calc_p1_bet(self.recovery_deficit)
                        self.last_event = f"Placing bets — P1={self.p1_bet:.2f} KES, P2=1.00 KES"
                        if self.p1_bet != 1:
                            await self._set_panel1_bet(frame, self.p1_bet)
                        prev_history = await get_crash_history(frame)
                        placed = await self.place_bets(frame)
                    except Exception as e:
                        self.log.warning("Frame stale placing bet (%s) — skipping round.", e)
                        rounds_left -= 1
                        continue

                    if not placed:
                        self.log.warning("Could not place bets — skipping round.")
                        rounds_left -= 1
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
                        round_pnl, desc = calc_round_pnl(crash_mult, self.p1_bet)
                        session_pnl         += round_pnl
                        self.cumulative_pnl += round_pnl
                        self.total_rounds   += 1
                        rounds_left         -= 1

                        # Update recovery deficit; reset only when P1 hits 6x
                        if crash_mult >= config.PANEL1_CASHOUT:
                            self.log.info(
                                "P1 won at %.2fx — recovery complete (deficit was %.2f KES).",
                                crash_mult, self.recovery_deficit,
                            )
                            self.recovery_deficit = 0.0
                        else:
                            # Deficit grows by any net loss this round
                            self.recovery_deficit = max(0.0, self.recovery_deficit - round_pnl)
                            self.log.info(
                                "Recovery deficit = %.2f KES → next P1 bet = %.2f KES.",
                                self.recovery_deficit, calc_p1_bet(self.recovery_deficit),
                            )

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
                        # Won this round — stop immediately, wait for next trigger
                        self.log.info(
                            "WIN this round (+%.2f KES) — returning to WATCH mode. "
                            "Session total: %.2f KES.  Recovery deficit: %.2f KES.",
                            round_pnl, session_pnl, self.recovery_deficit,
                        )
                        bet_next, watching = False, True
                        session_pnl = 0.0
                        # Reset P1 UI to 1 KES while watching (deficit still carries forward)
                        if self.p1_bet != 1:
                            try:
                                await self._set_panel1_bet(frame, 1)
                                self.p1_bet = 1
                            except Exception:
                                pass

                    elif rounds_left <= 0:
                        # Used all 4 rounds without a win — take the loss
                        self.log.info(
                            "All %d rounds used, no win. Session P&L = %.2f KES — "
                            "back to WATCH mode.  "
                            "Recovery deficit carries: %.2f KES → next P1 bet = %.2f KES.",
                            config.MAX_BET_ROUNDS, session_pnl,
                            self.recovery_deficit, calc_p1_bet(self.recovery_deficit),
                        )
                        bet_next, watching = False, True
                        session_pnl = 0.0
                        # Reset P1 UI to 1 KES while watching
                        if self.p1_bet != 1:
                            try:
                                await self._set_panel1_bet(frame, 1)
                                self.p1_bet = 1
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

                    # ── Trigger conditions ────────────────────────────────────
                    trigger_high = crash_mult > config.TRIGGER_MULT
                    recent8 = history[:8]
                    trigger_low8 = (
                        len(recent8) >= 8
                        and all(m <= config.LOW_STREAK_MAX for m in recent8)
                    )

                    if trigger_high:
                        trigger_reason = f"last crash {crash_mult:.2f}x > {config.TRIGGER_MULT:.1f}x"
                    elif trigger_low8:
                        trigger_reason = (
                            f"last 8 crashes all ≤ {config.LOW_STREAK_MAX:.1f}x "
                            f"({[round(m,2) for m in recent8]})"
                        )
                    else:
                        trigger_reason = None

                    self.last_event = f"Watching — last crash {crash_mult:.2f}x | total={self.cumulative_pnl:.2f} KES"
                    self.log.info(
                        "WATCH | crash=%.2fx | trigger_high=%s | low8=%s",
                        crash_mult, trigger_high, trigger_low8,
                    )

                    if trigger_reason:
                        self.last_event = f"TRIGGER: {trigger_reason}"
                        self.log.info(
                            "TRIGGER HIT (%s) — betting next %d round(s)!",
                            trigger_reason, config.MAX_BET_ROUNDS,
                        )
                        bet_next     = True
                        rounds_left  = config.MAX_BET_ROUNDS
                        session_pnl  = 0.0

        except KeyboardInterrupt:
            self.log.info("Interrupted by user.")
        except Exception as e:
            self.log.exception("Unhandled error: %s", e)
        finally:
            self._print_summary()
            self.csv.close()
            await self.logout()
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
