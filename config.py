"""
Aviator Bot Configuration
Edit these values before running the bot.
"""

# ── Credentials ───────────────────────────────────────────────────────────────
USERNAME = "0701347307"
PASSWORD = "27837185Qq!!!!!"

# ── Bet sizing ────────────────────────────────────────────────────────────────
BET_AMOUNT      = 50       # KES base bet for Panel 1
P2_BET_AMOUNT   = 50      # KES base bet for Panel 2 (can differ from BET_AMOUNT)

# ── Auto cashout targets (set once in the game UI, not touched again) ─────────
PANEL1_CASHOUT  = 3.2     # P1: strict low-streak entry
PANEL2_CASHOUT  = 4.0     # P2: rare >20x high-crash entry

# ── Recovery calculation ──────────────────────────────────────────────────────
RECOVERY_ENABLED          = False  # Flat staking only; no martingale on P1
RECOVERY_PROFIT_TARGET    = 25    # KES profit margin for P1 recovery formula
RECOVERY_SCOPE            = "percentage"   # "individual" | "combined" | "percentage" | "smart"
                                      # smart: P1 bets to cover both deficits; P1 win clears both
RECOVERY_PERCENTAGE       = 50  # % of total deficit P1 tries to recover per win (percentage scope)
RECOVERY_STEPS            = 2    # rounds to apply % recovery (0 = use MAX_BET_ROUNDS)
P2_RECOVERY_ENABLED       = False  # Flat staking only; no martingale on P2
P2_RECOVERY_PROFIT_TARGET = 25   # KES profit margin for P2 recovery formula
P2_RECOVERY_SCOPE         = "percentage"   # "individual" | "combined" | "percentage" | "smart"
                                      # smart: P2 bets only its own deficit; P2 win clears only P2
P2_RECOVERY_PERCENTAGE    = 50  # % of deficit P2 tries to recover per P2 win
P2_RECOVERY_STEPS         = 2    # rounds to apply P2 % recovery (0 = use MAX_BET_ROUNDS)
P2_ASSIST_P1_ENABLED      = False  # Keep P2 fully independent from P1
P2_ASSIST_PERCENTAGE      = 100   # % of P1 deficit P2 targets per win while assisting (0-100)

# ── Burst safety limits ───────────────────────────────────────────────────────
BURST_COOLDOWN             = 0   # Watch rounds to skip after each burst (0 = no cooldown)
STOP_ON_CONSECUTIVE_LOSSES = 0   # Stop session after N consecutive round losses (0 = off)

# ── P1 trigger (strict low-streak setup) ─────────────────────────────────────
P1_TRIGGER_MULT     = 999.0  # Disable high-crash trigger for P1
P1_LOW_STREAK_MAX   = 1.8    # Trigger P1 when recent crashes all stay at/below this
P1_LOW_STREAK_COUNT = 5      # How many consecutive low crashes needed to trigger P1
P1_BET_PATTERN      = [0, 1] # True alternate entry: skip the next round, then bet
P1_MAX_BET_ROUNDS   = 1      # Actual number of P1 bets inside the pattern

# ── P2 trigger (rare high-crash setup) ───────────────────────────────────────
P2_TRIGGER_MULT     = 20.0   # Trigger P2 only after a crash above this
P2_LOW_STREAK_MAX   = 0.0    # Disable P2 low-streak trigger
P2_LOW_STREAK_COUNT = 999    # Keep unreachable so only the high trigger can fire
P2_BET_PATTERN      = [1]    # Bet the very next round after the trigger
P2_MAX_BET_ROUNDS   = 1      # Actual number of P2 bets inside the pattern

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
