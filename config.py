"""
Aviator Bot Configuration
Edit these values before running the bot.
"""

# ── Credentials ───────────────────────────────────────────────────────────────
USERNAME = "0701347307"
PASSWORD = "27837185Qq!!!!!"

# ── Bet sizing ────────────────────────────────────────────────────────────────
BET_AMOUNT      = 50       # KES base bet for Panel 1
P2_BET_AMOUNT   = 50       # KES base bet for Panel 2 (can differ from BET_AMOUNT)

# ── Auto cashout targets (set once in the game UI, not touched again) ─────────
PANEL1_CASHOUT  = 2.2     # P1: lower target for steadier cold-streak hits
PANEL2_CASHOUT  = 3.0     # P2: same trigger, higher payout companion

# ── Recovery calculation ──────────────────────────────────────────────────────
RECOVERY_ENABLED          = False  # Flat staking only; backtest favored lower drawdown
RECOVERY_PROFIT_TARGET    = 25    # KES profit margin for P1 recovery formula
RECOVERY_SCOPE            = "individual"   # "individual" | "combined" | "percentage" | "smart"
                                      # smart: P1 bets to cover both deficits; P1 win clears both
RECOVERY_PERCENTAGE       = 50  # % of total deficit P1 tries to recover per win (percentage scope)
RECOVERY_STEPS            = 2    # rounds to apply % recovery (0 = use MAX_BET_ROUNDS)
P1_ASSIST_P2_ENABLED      = False  # Recovery is off; keep panels independent
P1_ASSIST_PERCENTAGE      = 50    # % of P2 deficit P1 targets per assist win (0-100)
P2_RECOVERY_ENABLED       = False  # Flat staking only; avoid compounding losing streaks
P2_RECOVERY_PROFIT_TARGET = 25   # KES profit margin for P2 recovery formula
P2_RECOVERY_SCOPE         = "individual"   # "individual" | "combined" | "percentage" | "smart"
                                      # smart: P2 bets only its own deficit; P2 win clears only P2
P2_RECOVERY_PERCENTAGE    = 100  # % of deficit P2 tries to recover per P2 win
P2_RECOVERY_STEPS         = 2    # rounds to apply P2 % recovery (0 = use MAX_BET_ROUNDS)
P2_ASSIST_P1_ENABLED      = False  # Keep P2 fully independent from P1
P2_ASSIST_PERCENTAGE      = 100   # % of P1 deficit P2 targets per win while assisting (0-100)

# ── Burst safety limits ───────────────────────────────────────────────────────
BURST_COOLDOWN             = 1   # Watch rounds to skip after each burst — prevents chain-triggering
STOP_ON_CONSECUTIVE_LOSSES = 0   # Stop session after N consecutive round losses (0 = off)

# ── P1 trigger ────────────────────────────────────────────────────────────────
# High trigger is disabled. Backtest showed the 9x-18x band was the main leak.
P1_TRIGGER_MULT     = 999.0  # Lower bound equals max, so no high-crash trigger can fire
P1_TRIGGER_MULT_MAX = 999.0
P1_LOW_STREAK_MAX   = 2.5    # Trigger when recent crashes all stay at/below this
P1_LOW_STREAK_COUNT = 8      # Consecutive low crashes needed to trigger
P1_BET_PATTERN      = [0, 1] # Skip one round after the trigger, then bet once
P1_MAX_BET_ROUNDS   = 1      # One actual P1 betting step inside the pattern

# ── P2 trigger ────────────────────────────────────────────────────────────────
P2_TRIGGER_MULT     = 999.0  # High trigger disabled
P2_TRIGGER_MULT_MAX = 999.0
P2_LOW_STREAK_MAX   = 2.5    # Same streak threshold as P1
P2_LOW_STREAK_COUNT = 8      # Same count as P1
P2_BET_PATTERN      = [0, 1] # Skip one round after the trigger, then bet once
P2_MAX_BET_ROUNDS   = 1      # One actual P2 betting step inside the pattern

# ── Global session guards ─────────────────────────────────────────────────────
STOP_ON_PROFIT  = 50000    # Stop entire bot when total profit >= this (KES)
STOP_ON_LOSS    = -50000    # Stop entire bot when total loss <= this (KES)
INITIAL_DEMO_BALANCE = 50000  # Starting bankroll for Demo mode; set to 0/None to auto-detect from UI

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
