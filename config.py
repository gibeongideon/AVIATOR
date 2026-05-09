"""
Aviator Bot Configuration
Edit these values before running the bot.
"""

# ── Credentials ───────────────────────────────────────────────────────────────
USERNAME = "0701347307"
PASSWORD = "27837185Qq!!!!!"

# ── Bet sizing ────────────────────────────────────────────────────────────────
BET_AMOUNT      = 1       # KES per panel per round (always 1 bob)

# ── Auto cashout targets (set once in the game UI, not touched again) ─────────
PANEL1_CASHOUT  = 6.0     # Panel 1 cashes out at 6x
PANEL2_CASHOUT  = 3.0     # Panel 2 cashes out at 3x

# ── Recovery calculation ──────────────────────────────────────────────────────
RECOVERY_PROFIT_TARGET = 1    # KES profit margin added on top of deficit before dividing by odds
                              # e.g. deficit=16 → bet = ceil((16 + 1) / 6) = 3

# ── Strategy trigger ──────────────────────────────────────────────────────────
TRIGGER_MULT    = 9.0     # Start betting when the last crash was above this
LOW_STREAK_MAX  = 3.0     # Also trigger when ALL of the last 8 crashes stayed at/below this
MAX_BET_ROUNDS  = 4       # Bet at most this many rounds per session

# ── Global session guards ─────────────────────────────────────────────────────
STOP_ON_PROFIT  = 500     # Stop entire bot when total profit >= this (KES)
STOP_ON_LOSS    = -200    # Stop entire bot when total loss <= this (KES)

# ── Browser settings ──────────────────────────────────────────────────────────
HEADLESS        = False   # True = invisible Chrome
SLOW_MO         = 80      # ms delay between actions
BROWSER_TIMEOUT = 30_000  # ms — global timeout

# ── URLs ──────────────────────────────────────────────────────────────────────
BASE_URL    = "https://www.ke.sportpesa.com"
LOGIN_URL   = f"{BASE_URL}/login"
AVIATOR_URL = f"{BASE_URL}/en/casino/aviator"

