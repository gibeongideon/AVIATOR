"""
Aviator Bot Configuration
Edit these values before running the bot.
"""

# ── Credentials ──────────────────────────────────────────────────────────────
USERNAME = "your_sportpesa_phone_or_email"
PASSWORD = "your_password"

# ── Betting strategy ─────────────────────────────────────────────────────────
BET_AMOUNT          = 10          # KES per round
AUTO_CASHOUT_AT     = 1.50        # cash out when multiplier reaches this value
MAX_ROUNDS          = 20          # stop after this many rounds (None = run forever)
STOP_ON_LOSS_STREAK = 5           # stop if we lose this many rounds in a row
STOP_ON_PROFIT      = 500         # stop when cumulative profit >= this (KES)
STOP_ON_LOSS        = -200        # stop when cumulative loss <= this (KES)

# ── Browser settings ─────────────────────────────────────────────────────────
HEADLESS            = False       # True = invisible Chrome, False = visible window
SLOW_MO             = 100         # ms delay between actions (helps avoid detection)
BROWSER_TIMEOUT     = 30_000      # ms — global timeout for page actions

# ── URLs ─────────────────────────────────────────────────────────────────────
BASE_URL    = "https://www.ke.sportpesa.com"
LOGIN_URL   = f"{BASE_URL}/en/login"
AVIATOR_URL = f"{BASE_URL}/en/casino/aviator"
