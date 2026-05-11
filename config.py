"""
Aviator Bot Configuration
Edit these values before running the bot.
"""

# ── Credentials ───────────────────────────────────────────────────────────────
USERNAME = "0701347307"
PASSWORD = "27837185Qq!!!!!"

# ── Bet sizing ────────────────────────────────────────────────────────────────
BET_AMOUNT      = 10       # KES base bet for Panel 1
P2_BET_AMOUNT   = 10      # KES base bet for Panel 2 (can differ from BET_AMOUNT)

# ── Auto cashout targets (set once in the game UI, not touched again) ─────────
PANEL1_CASHOUT  = 6.0     # Panel 1 cashes out at 6x
PANEL2_CASHOUT  = 3.0     # Panel 2 cashes out at 3x

# ── Recovery calculation ──────────────────────────────────────────────────────
RECOVERY_ENABLED          = True  # False = P1 always bets flat BET_AMOUNT (no scaling)
RECOVERY_PROFIT_TARGET    = 10    # KES profit margin for P1 recovery formula
RECOVERY_SCOPE            = "smart"   # "individual" | "combined" | "percentage" | "smart"
                                      # smart: P1 bets to cover both deficits; P1 win clears both
RECOVERY_PERCENTAGE       = 100  # % of total deficit P1 tries to recover per win (percentage scope)
RECOVERY_STEPS            = 0    # rounds to apply % recovery (0 = use MAX_BET_ROUNDS)
P2_RECOVERY_ENABLED       = True   # True = P2 also uses martingale (independent of P1)
P2_RECOVERY_PROFIT_TARGET = 10    # KES profit margin for P2 recovery formula
P2_RECOVERY_SCOPE         = "smart"   # "individual" | "combined" | "percentage" | "smart"
                                      # smart: P2 bets only its own deficit; P2 win clears only P2
P2_RECOVERY_PERCENTAGE    = 50  # % of deficit P2 tries to recover per P2 win
P2_RECOVERY_STEPS         = 0    # rounds to apply P2 % recovery (0 = use MAX_BET_ROUNDS)
P2_ASSIST_P1_ENABLED      = True  # when P1 has deficit, let P2 assist even if P2 also has deficit
P2_ASSIST_PERCENTAGE      = 100   # % of P1 deficit P2 targets per win while assisting (0-100)

# ── Burst safety limits ───────────────────────────────────────────────────────
BURST_COOLDOWN             = 0   # Watch rounds to skip after each burst (0 = no cooldown)
STOP_ON_CONSECUTIVE_LOSSES = 0   # Stop session after N consecutive round losses (0 = off)

# ── P1 trigger (independent) ─────────────────────────────────────────────────
P1_TRIGGER_MULT     = 8.0  # Bet P1 when last crash exceeds this
P1_LOW_STREAK_MAX   = 3.0  # Also trigger P1 when recent crashes all stay at/below this
P1_LOW_STREAK_COUNT = 8    # How many consecutive low crashes needed to trigger P1
P1_MAX_BET_ROUNDS   = 5   # P1 bets at most this many rounds per burst

# ── P2 trigger (independent) ─────────────────────────────────────────────────
P2_TRIGGER_MULT     = 8.0  # Bet P2 when last crash exceeds this
P2_LOW_STREAK_MAX   = 3.0  # Also trigger P2 when recent crashes all stay at/below this
P2_LOW_STREAK_COUNT = 8    # How many consecutive low crashes needed to trigger P2
P2_MAX_BET_ROUNDS   = 5    # P2 bets at most this many rounds per burst

# ── Global session guards ─────────────────────────────────────────────────────
STOP_ON_PROFIT  = 50000    # Stop entire bot when total profit >= this (KES)
STOP_ON_LOSS    = -50000    # Stop entire bot when total loss <= this (KES)
INITIAL_DEMO_BALANCE = 50000.0  # Starting bankroll for Demo mode; set to 0/None to auto-detect from UI

# ── Admin panel ───────────────────────────────────────────────────────────────
import os as _os
ADMIN_PASSWORD = _os.getenv("ADMIN_PASSWORD", "aviator-admin-2026")  # change or set env var

# ── Browser settings ──────────────────────────────────────────────────────────
HEADLESS        = True    # True = invisible Chrome (recommended/default for server use)
SLOW_MO         = 80      # ms delay between actions
BROWSER_TIMEOUT = 30_000  # ms — global timeout
DEMO_MODE       = True   # True = click "Demo" in Spribe instead of real money
AUTO_LOGOUT     = True    # True = log out of SportPesa when the bot stops

# ── URLs ──────────────────────────────────────────────────────────────────────
BASE_URL    = "https://www.ke.sportpesa.com"
LOGIN_URL   = f"{BASE_URL}/login"
AVIATOR_URL = f"{BASE_URL}/en/casino/aviator"
