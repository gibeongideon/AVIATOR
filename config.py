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
PANEL1_CASHOUT  = 2.5     # P1: lower recovery target and first priority
PANEL2_CASHOUT  = 3.5     # P2: higher recovery target when P1 is not recovering

# ── Recovery calculation ──────────────────────────────────────────────────────
RECOVERY_ENABLED          = True   # P1 recovery enabled; P1 clears all deficits in smart mode
RECOVERY_PROFIT_TARGET    = 25    # KES profit margin for P1 recovery formula
RECOVERY_SCOPE            = "smart"   # "individual" | "combined" | "percentage" | "smart"
                                      # smart: P1 bets to cover both deficits; P1 win clears both
RECOVERY_PERCENTAGE       = 50  # % of total deficit P1 tries to recover per win (percentage scope)
RECOVERY_STEPS            = 2    # rounds to apply % recovery (0 = use MAX_BET_ROUNDS)
P1_ASSIST_P2_ENABLED      = True  # Let P1 assist P2 during very low-crash recovery pressure
P1_ASSIST_PERCENTAGE      = 100    # % of P2 deficit P1 targets per assist win (0-100)
P1_ASSIST_TRIGGER_MAX     = 1.4   # P1 assists P2 when previous crash is <= this value
P1_ASSIST_CASHOUT         = 1.4   # Temporary P1 cashout used only for P2 assist rounds
P2_RECOVERY_ENABLED       = True   # P2 recovers only when P1 is not already recovering
P2_RECOVERY_PROFIT_TARGET = 25   # KES profit margin for P2 recovery formula
P2_RECOVERY_SCOPE         = "combined"   # "individual" | "combined" | "percentage" | "smart"
                                      # combined: P2 can recover total deficit when P1 is not leading
P2_RECOVERY_PERCENTAGE    = 100  # % of deficit P2 tries to recover per P2 win
P2_RECOVERY_STEPS         = 2    # rounds to apply P2 % recovery (0 = use MAX_BET_ROUNDS)
P2_ASSIST_P1_ENABLED      = False  # P1 has priority because its cashout is lower
P2_ASSIST_PERCENTAGE      = 100   # % of P1 deficit P2 targets per win while assisting (0-100)

# ── Burst safety limits ───────────────────────────────────────────────────────
BURST_COOLDOWN             = 0   # Let the next qualifying previous crash re-trigger recovery
STOP_ON_CONSECUTIVE_LOSSES = 0   # Stop session after N consecutive round losses (0 = off)

# ── P1 trigger ────────────────────────────────────────────────────────────────
# P1 recovery is triggered by the previous crash being greater than 2.5x.
P1_TRIGGER_MULT     = 2.5
P1_TRIGGER_MULT_MAX = float("inf")
P1_LOW_STREAK_MAX   = 0.0    # Disable low-streak trigger; crashes are positive
P1_LOW_STREAK_COUNT = 1
P1_BET_PATTERN      = [1]    # Bet the next round after the trigger
P1_MAX_BET_ROUNDS   = 1      # One actual P1 betting step inside the pattern

# ── P2 trigger ────────────────────────────────────────────────────────────────
# P2 recovery is triggered by the previous crash being less than 3.5x, but if P1
# is also recovering in the overlap band, P2 only places the normal base bet.
P2_TRIGGER_MULT     = 3.5
P2_TRIGGER_MULT_MAX = 0.0    # P2 uses the lower-than trigger below
P2_LOW_STREAK_MIN   = 1.4    # P2 recovery trigger requires crash > this and < P2_LOW_STREAK_MAX
P2_LOW_STREAK_MAX   = 3.5
P2_LOW_STREAK_COUNT = 1
P2_BET_PATTERN      = [1]    # Bet the next round after the trigger
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
