"""
Aviator Bot Configuration
Edit these values before running the bot.
"""

# ── Credentials ───────────────────────────────────────────────────────────────
USERNAME = "0769024170"
PASSWORD = "27837185Qq!!!!!"

# ── Bet sizing ────────────────────────────────────────────────────────────────
BET_AMOUNT      = 1       # KES base bet for Panel 1
P2_BET_AMOUNT   = 1       # KES base bet for Panel 2 (can differ from BET_AMOUNT)

# ── Auto cashout targets (set once in the game UI, not touched again) ─────────
PANEL1_CASHOUT  = 6.0     # Panel 1 cashes out at 6x
PANEL2_CASHOUT  = 3.0     # Panel 2 cashes out at 3x

# ── Recovery calculation ──────────────────────────────────────────────────────
RECOVERY_ENABLED          = True  # False = P1 always bets flat BET_AMOUNT (no scaling)
RECOVERY_PROFIT_TARGET    = 5    # KES profit margin for P1 recovery formula
RECOVERY_SCOPE            = "individual"  # "individual" | "combined" | "percentage"
RECOVERY_PERCENTAGE       = 100  # % of total deficit P1 tries to recover per win (percentage scope)
RECOVERY_STEPS            = 0    # rounds to apply % recovery (0 = use MAX_BET_ROUNDS)
P2_RECOVERY_ENABLED       = False  # True = P2 also uses martingale (independent of P1)
P2_RECOVERY_PROFIT_TARGET = 5    # KES profit margin for P2 recovery formula
P2_RECOVERY_SCOPE         = "individual"  # "individual" | "combined" | "percentage"
P2_RECOVERY_PERCENTAGE    = 100  # % of deficit P2 tries to recover per P2 win
P2_RECOVERY_STEPS         = 0    # rounds to apply P2 % recovery (0 = use MAX_BET_ROUNDS)

# ── Burst safety limits ───────────────────────────────────────────────────────
BURST_COOLDOWN             = 0   # Watch rounds to skip after each burst (0 = no cooldown)
STOP_ON_CONSECUTIVE_LOSSES = 0   # Stop session after N consecutive round losses (0 = off)

# ── Strategy trigger ──────────────────────────────────────────────────────────
TRIGGER_MULT    = 9.0     # Start betting when the last crash was above this
LOW_STREAK_MAX  = 3.0     # Also trigger when ALL of the last 8 crashes stayed at/below this
MAX_BET_ROUNDS  = 4       # Bet at most this many rounds per session

# ── Global session guards ─────────────────────────────────────────────────────
STOP_ON_PROFIT  = 500     # Stop entire bot when total profit >= this (KES)
STOP_ON_LOSS    = -200    # Stop entire bot when total loss <= this (KES)

# ── Admin panel ───────────────────────────────────────────────────────────────
import os as _os
ADMIN_PASSWORD = _os.getenv("ADMIN_PASSWORD", "aviator-admin-2026")  # change or set env var

# ── Browser settings ──────────────────────────────────────────────────────────
HEADLESS        = False   # True = invisible Chrome
SLOW_MO         = 80      # ms delay between actions
BROWSER_TIMEOUT = 30_000  # ms — global timeout
DEMO_MODE       = False   # True = click "Demo" in Spribe instead of real money
AUTO_LOGOUT     = True    # True = log out of SportPesa when the bot stops

# ── URLs ──────────────────────────────────────────────────────────────────────
BASE_URL    = "https://www.ke.sportpesa.com"
LOGIN_URL   = f"{BASE_URL}/login"
AVIATOR_URL = f"{BASE_URL}/en/casino/aviator"

