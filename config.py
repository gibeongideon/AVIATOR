"""
Aviator Bot Configuration
Edit these values before running the bot.
"""

# ── Credentials ───────────────────────────────────────────────────────────────
USERNAME = "0769024170"
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
P1_ASSIST_P2_ENABLED      = False  # Let P1 assist P2 during very low-crash recovery pressure
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
RECOVERY_CHUNK_CAP         = 0     # KES — fixed max deficit recovered per P1 win (0 = disabled; use PCT below instead)
RECOVERY_CHUNK_CAP_PCT     = 10    # % of INITIAL_BALANCE to use as chunk cap per P1 win (0 = use fixed KES above)
INITIAL_BALANCE            = 30000 # KES — your starting bankroll; required when RECOVERY_CHUNK_CAP_PCT > 0 (0 = fallback to fixed KES cap)

# ── Global trigger gate ───────────────────────────────────────────────────────
MIN_TRIGGER_CRASH   = 1.22    # Skip ALL triggers if previous crash was below this (0 = disabled)

# ── P1 low-zone recovery ──────────────────────────────────────────────────────
# When prev crash is between MIN_TRIGGER_CRASH and P1_LOW_ZONE_MAX, P1 bets at
# a lower cashout to chip away at its deficit (instead of sitting out).
P1_LOW_ZONE_ENABLED    = False  # Enable P1 low-zone recovery (default: off)
P1_LOW_ZONE_MAX        = 1.4    # Upper bound of low zone (lower bound = MIN_TRIGGER_CRASH)
P1_LOW_ZONE_CASHOUT    = 1.5    # Cashout target for low-zone bets
P1_LOW_ZONE_PERCENTAGE = 50     # % of P1 deficit to target per low-zone win (0 = base bet only)

# ── Follow (idle-fill) settings ───────────────────────────────────────────────
P1_FOLLOW_P2        = False  # P1 places base bet when P2 triggers alone (prev crash 1.22x–2.5x)
P2_FOLLOW_P1        = False  # P2 places base bet when P1 triggers alone (prev crash > 3.5x)

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

# ── Anti-Martingale strategy (optional mode — replaces P1/P2 recovery) ────────
# When enabled: bet on P1 after prev crash ≥ AM_TRIGGER_CRASH.
# Bet doubles after each win up to AM_MAX_STREAK consecutive wins, then resets.
# A loss always resets back to base bet. P2 stays idle.
AM_STRATEGY_ENABLED = False      # True activates AM mode; disables P1/P2 recovery
AM_TRIGGER_CRASH    = 8.0        # Bet next round when previous crash >= this
AM_CASHOUT          = 7.0        # Auto-cashout multiplier for AM bets
AM_BET_AMOUNT       = 50.0       # Base unit bet (KES)
AM_MAX_STREAK       = 4          # Max consecutive win doublings (4 → bets ×1, ×2, ×4, ×8 then reset)
AM_MAX_BET          = 5000.0     # Hard bet cap (KES)

# ── P2 Anti-Martingale (concurrent with P1 AM; independent toggle) ─────────────
# Runs only when AM_STRATEGY_ENABLED = True. P2 gets its own trigger and
# anti-martingale sequence, completely independent from P1.
# Second-best backtest: prev≥8x @ 8x → +2.32% edge, +21,700 KES flat over 18,927 rounds.
P2_AM_ENABLED       = False      # True: P2 also runs AM alongside P1 AM
P2_AM_TRIGGER_CRASH = 8.0        # P2 bets when previous crash >= this
P2_AM_CASHOUT       = 8.0        # P2 auto-cashout (different from P1's 7x)
P2_AM_BET_AMOUNT    = 50.0       # P2 base bet (KES)
P2_AM_MAX_STREAK    = 4          # P2 max consecutive win doublings
P2_AM_MAX_BET       = 5000.0     # P2 hard bet cap (KES)

# ── Global session guards ─────────────────────────────────────────────────────
STOP_ON_PROFIT       = 3000  # Stop entire bot when total profit >= this (KES)
STOP_ON_LOSS         = 0     # Stop entire bot when total loss <= this (KES); 0 = disabled
STOP_ON_DRAWDOWN_PCT = 20    # Stop if PnL drops by X% from its session peak (0 = disabled)
                              # e.g. 20 means: if peak was +3000, stop if PnL falls to +2400
DRAWDOWN_PROTECTION_PCT = 10.0  # Switch to conservative recovery when balance drops this %
                                 # of INITIAL_BALANCE below the session peak (0 = disabled).
                                 # e.g. 10 → for 50k initial: 5k floor; peak 60k → floor 55k.
                                 # Uses INITIAL_DEMO_BALANCE in demo mode, INITIAL_BALANCE otherwise.
                                 # Bot keeps betting but caps recovery target at the threshold KES.
STOP_PROFIT_LOSS_FRAC     = 0.25 # After STOP_ON_PROFIT is reached: if a single betting round
                                  # loses more than this fraction of the peak, stop immediately.
                                  # e.g. 0.25 means: peak=4000 at target, stop if one round > 1000 KES
                                  # Set to 0 to disable.
STOP_PROFIT_LOSS_FRAC_MAX = 0.50 # Fraction scales UP as profit grows beyond STOP_ON_PROFIT.
                                  # At 1× target → STOP_PROFIT_LOSS_FRAC (0.25)
                                  # At 2× target → STOP_PROFIT_LOSS_FRAC_MAX (0.50) — full allowance
                                  # Linear ramp between; capped at MAX beyond 2× target.
                                  # e.g. peak=8000 with target=3000 → allows up to 4000 KES per round.
INITIAL_DEMO_BALANCE = 50000  # Starting bankroll for Demo mode; set to 0/None to auto-detect from UI

# ── ML Predictor (optional confidence gate) ──────────────────────────────────
# When enabled, the predictor trains in a background thread on all historical
# crash data and retrains every PREDICTOR_RETRAIN_ROUNDS new rounds.
# P1/P2 triggers are suppressed when P(win) < (1/cashout) × PREDICTOR_Px_CONFIDENCE.
#   0   = gate disabled (bet whenever trigger fires regardless of predictor)
#   1.0 = require at least break-even probability         (positive EV filter)
#   1.05= require 5 % above break-even                   (tighter filter)
PREDICTOR_ENABLED          = True
PREDICTOR_RETRAIN_ROUNDS   = 500   # new rounds per session before next retrain
PREDICTOR_MIN_ROUNDS       = 1000  # minimum history to train on
PREDICTOR_P1_CONFIDENCE    = 1.0   # gate multiplier for P1 (recovery mode only)
PREDICTOR_P2_CONFIDENCE    = 1.0   # gate multiplier for P2 (recovery mode only)

# ── Auto-restart ───────────────────────────────────────────────────────────────
AUTO_RESTART_SESSION = True   # Automatically start a new session after stop (profit/drawdown/loss)
RESTART_DELAY        = 10     # Seconds to wait between sessions (0 = immediate)

# ── Admin panel ───────────────────────────────────────────────────────────────
import os as _os
ADMIN_PASSWORD = _os.getenv("ADMIN_PASSWORD", "aviator-admin-2026")  # change or set env var

# ── Browser settings ──────────────────────────────────────────────────────────
HEADLESS        = True    # True = invisible Chrome (recommended/default for server use)
SLOW_MO         = 80      # ms delay between actions
BROWSER_TIMEOUT = 30_000  # ms — global timeout
DEMO_MODE       = True  # True = click "Demo" in Spribe instead of real money
AUTO_LOGOUT     = True    # True = log out of SportPesa when the bot stops

# ── URLs ──────────────────────────────────────────────────────────────────────
BASE_URL    = "https://www.ke.sportpesa.com"
LOGIN_URL   = f"{BASE_URL}/login"
AVIATOR_URL = f"{BASE_URL}/en/casino/aviator"
