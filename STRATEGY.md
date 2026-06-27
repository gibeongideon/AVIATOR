# Aviator Bot — Strategy Reference

This document explains every strategy mode and risk-control mechanism in the bot.
All values are set in `config.py`. Both `bot.py` (local) and `src/bot.py` (server) run identical logic.

---

## Overview: Two Strategy Modes

| Mode | Config switch | What it does |
|---|---|---|
| **Recovery** (default) | `AM_STRATEGY_ENABLED = False` | Two-panel deficit-recovery system. Tracks losses and sizes bets to recover them. |
| **Anti-Martingale** | `AM_STRATEGY_ENABLED = True` | Single-panel momentum betting. No deficit tracking. Doubles bet after each win. |

---

## Mode 1 — Recovery Strategy (Default)

### How it works

The bot watches the crash history and uses **two independent panels** (P1 and P2) each with its own trigger, cashout target, and deficit tracker. A "deficit" is the cumulative loss the panel needs to recover.

```
After each round that is bet:
  Win  → profit credited, deficit cleared (up to the chunk cap)
  Loss → loss added to deficit; next bet is sized to recover deficit + profit target
```

### Bet Sizing Formula

```
P1 bet = (deficit + RECOVERY_PROFIT_TARGET) / (PANEL1_CASHOUT − 1)
```

Example with current defaults:
- Deficit = 500 KES, profit target = 25, cashout = 2.5x → net = 1.5
- Bet = (500 + 25) / 1.5 = **350 KES**

P2 uses the same formula with `P2_BET_AMOUNT`, `PANEL2_CASHOUT`, `P2_RECOVERY_PROFIT_TARGET`.

### Triggers

Each panel triggers independently based on the **previous round's crash multiplier**.

#### Panel 1 (P1)
| Condition | Trigger fires when |
|---|---|
| High crash | `prev crash > P1_TRIGGER_MULT` (default 2.5x) |
| Low streak | Last `P1_LOW_STREAK_COUNT` crashes all ≤ `P1_LOW_STREAK_MAX` |
| P2 assist | `P1_ASSIST_P2_ENABLED = True` and prev crash ≤ `P1_ASSIST_TRIGGER_MAX` |
| Low zone | `P1_LOW_ZONE_ENABLED = True` and prev crash ≤ `P1_LOW_ZONE_MAX` |

**Current config**: P1 triggers when `prev crash > 2.5x`. Low streak disabled (`P1_LOW_STREAK_MAX = 0`).

#### Panel 2 (P2)
| Condition | Trigger fires when |
|---|---|
| Low band | `P2_LOW_STREAK_MIN < prev crash < P2_LOW_STREAK_MAX` (1.4x–3.5x) |

**Current config**: P2 triggers when `1.4x < prev crash < 3.5x`.

#### Global gate
`MIN_TRIGGER_CRASH = 1.22` — if `prev crash < 1.22x`, ALL triggers are skipped that round.

### Recovery Scope

`RECOVERY_SCOPE` controls how P1 handles combined deficits:

| Value | Behaviour |
|---|---|
| `individual` | P1 only recovers its own deficit |
| `combined` | P1 recovers P1 + P2 deficits together |
| `percentage` | P1 recovers a fixed % of total deficit per win |
| `smart` | Same as combined; P2 is suppressed (bets base only) while P1 is recovering |

**Current config**: `smart` — P1 covers both deficits, P2 suppressed when P1 leads.

`P2_RECOVERY_SCOPE = "combined"` — P2 can recover total deficit when P1 is not leading.

### Chunk Cap (Bet Limiter)

Prevents a single P1 win from attempting to recover an arbitrarily large accumulated deficit.

```
RECOVERY_CHUNK_CAP_PCT = 10   → cap = 10% of INITIAL_DEMO_BALANCE (50,000) = 5,000 KES
```

Max P1 bet = `(5,000 + 50 + 25) / (2.5 − 1) = 3,383 KES`

If `RECOVERY_CHUNK_CAP = 0` and `RECOVERY_CHUNK_CAP_PCT = 0`, there is no cap — deficit recovery is unbounded.

### P1 Low-Zone Recovery

When the previous crash is in the range `(MIN_TRIGGER_CRASH, P1_LOW_ZONE_MAX]`, P1 can chip away at its deficit at a lower cashout instead of waiting.

```python
P1_LOW_ZONE_ENABLED    = False   # off by default
P1_LOW_ZONE_MAX        = 1.4
P1_LOW_ZONE_CASHOUT    = 1.5
P1_LOW_ZONE_PERCENTAGE = 50      # target 50% of deficit per low-zone win
```

### Follow (Idle-Fill)

| Setting | Effect |
|---|---|
| `P1_FOLLOW_P2 = True` | P1 places a base bet when P2 triggers alone |
| `P2_FOLLOW_P1 = True` | P2 places a base bet when P1 triggers alone |

Both default to `False`. Useful to keep both panels active every round.

### P1 Assists P2 / P2 Assists P1

```python
P1_ASSIST_P2_ENABLED = False   # P1 bets at a low cashout to help clear P2 deficit
P2_ASSIST_P1_ENABLED = False   # P2 bets to help clear P1 deficit
```

These add a secondary role for each panel. Disabled by default because P1 (lower cashout) naturally clears both deficits in `smart` scope.

---

## Mode 2 — Anti-Martingale Strategy

### Motivation

Grid search across 18,927 historical rounds found that **after a crash ≥ 8x, the next round is measurably more likely to reach 7x** than random chance:

| Condition | Win rate @ 7x | Break-even | Edge |
|---|---|---|---|
| All rounds (flat) | 14.29% | 14.29% | 0% |
| After prev ≥ 8x | **16.74%** | 14.29% | **+2.45%** |
| After prev ≥ 7x | 16.48% | 14.29% | +2.19% |
| After prev ≥ 5x | 15.92% | 14.29% | +1.63% |

Anti-Martingale amplifies this edge by betting bigger after a win (riding the variance up) and resetting immediately on a loss (cutting exposure).

### How it works

1. Watch the crash history; do nothing until `prev crash ≥ AM_TRIGGER_CRASH`
2. On trigger: queue a P1 bet for the **next round** at `AM_CASHOUT`
3. **Win** → increment streak, double the next bet (up to `AM_MAX_STREAK` consecutive wins, then reset to base)
4. **Loss** → reset streak and bet immediately to `AM_BET_AMOUNT`
5. P2 panel is idle unless `P2_AM_ENABLED = True` (see below)

### Bet sequence (AM_MAX_STREAK = 4, AM_BET_AMOUNT = 50)

```
Streak 0 (after loss / start) : 50 KES
Streak 1 (after 1 win)        : 100 KES
Streak 2 (after 2 wins)       : 200 KES
Streak 3 (after 3 wins)       : 400 KES   ← capped by AM_MAX_BET if needed
After 4 consecutive wins       : reset → 50 KES
```

A single loss at any level resets immediately to 50 KES.

### Performance (18,927-round backtest)

| Bet system | PnL | Worst balance | Max drawdown |
|---|---|---|---|
| Flat 50 KES | +20,100 KES | 45,100 | 7,000 |
| **Anti-Mart ×4** | **+37,500 KES** | **44,300** | **7,000** |
| Martingale | −182,100 KES | −193,450 | catastrophic |
| Fibonacci | −483,550 KES | −458,550 | catastrophic |

Anti-Martingale nearly **doubles flat-bet profit** with identical maximum drawdown. Martingale and Fibonacci are destroyed by losing streaks (max 44 consecutive losses observed).

### Config (P1 AM)

```python
AM_STRATEGY_ENABLED = False      # set True to activate
AM_TRIGGER_CRASH    = 8.0        # bet after prev crash >= this
AM_CASHOUT          = 7.0        # auto-cashout target
AM_BET_AMOUNT       = 50.0       # base bet (KES)
AM_MAX_STREAK       = 4          # max win doublings before reset
AM_MAX_BET          = 5000.0     # hard cap per bet (KES)
```

---

## Mode 2b — P2 Anti-Martingale (Concurrent)

Runs **only when `AM_STRATEGY_ENABLED = True`**. P2 gets its own independent trigger, cashout, and AM streak that operates in parallel with P1. Both panels bet concurrently on rounds where both triggers fire.

### Why run P2 concurrently?

The second-best edge condition found in the grid search:

| Condition | Win rate @ 8x | Break-even | Edge |
|---|---|---|---|
| All rounds (flat) | 12.50% | 12.50% | 0% |
| After prev ≥ 8x | **14.82%** | 12.50% | **+2.32%** |

Running P2 at 8x cashout concurrently with P1 at 7x nearly **doubles combined PnL** because both edges are uncorrelated — they are triggered by the same event but cashed out at different multipliers.

### How P2 AM works

- P2 watches the same crash history independently
- Trigger and AM progression are completely separate from P1
- On the same round where P1 is betting, P2 can also be active
- A P2 win/loss does not affect P1's streak and vice versa

### Combined Performance (18,955-round simulation)

| Configuration | PnL | Worst balance | Max drawdown |
|---|---|---|---|
| Flat 50 KES (P1 only) | +20,100 KES | 45,100 | 7,000 |
| **P1 AM only** (prev≥8x @7x) | **+37,150 KES** | **44,300** | **7,250** |
| **P1+P2 AM concurrent** (prev≥8x @7x + @8x) | **+77,500 KES** | **36,900** | **14,050** |

Trade-off: combined mode nearly doubles PnL but also doubles max drawdown (from ~7k to ~14k KES). Both panels losing simultaneously on the same trigger round amplifies downside as well as upside.

### Bet sequence (P2, same progression as P1)

```
Streak 0 (after loss / start) : 50 KES
Streak 1 (after 1 win)        : 100 KES
Streak 2 (after 2 wins)       : 200 KES
Streak 3 (after 3 wins)       : 400 KES
After 4 consecutive wins       : reset → 50 KES
```

### Config (P2 AM)

```python
P2_AM_ENABLED       = False      # True: P2 runs AM alongside P1 AM
P2_AM_TRIGGER_CRASH = 8.0        # P2 bets when previous crash >= this
P2_AM_CASHOUT       = 8.0        # P2 cashout (second-best: prev≥8x@8x → +2.32% edge)
P2_AM_BET_AMOUNT    = 50.0       # P2 base bet (KES)
P2_AM_MAX_STREAK    = 4          # P2 max consecutive win doublings
P2_AM_MAX_BET       = 5000.0     # P2 hard bet cap (KES)
```

P2 AM is disabled by default. Enable it only when `AM_STRATEGY_ENABLED = True` — it has no effect in recovery mode.

---

### Switching modes

To enable AM mode (P1 only):
```python
AM_STRATEGY_ENABLED = True
P2_AM_ENABLED       = False
```

To enable AM mode (P1 + P2 concurrent):
```python
AM_STRATEGY_ENABLED = True
P2_AM_ENABLED       = True
```

To return to recovery mode:
```python
AM_STRATEGY_ENABLED = False
```

AM mode and Recovery mode are mutually exclusive. AM mode completely bypasses the P1/P2 deficit system.

---

## Risk Controls (Both Modes)

### Session Stop Conditions

| Setting | Stops when |
|---|---|
| `STOP_ON_PROFIT = 3000` | cumulative PnL ≥ 3,000 KES |
| `STOP_ON_LOSS = 0` | disabled (set negative KES value to enable) |
| `STOP_ON_DRAWDOWN_PCT = 20` | PnL drops 20% from its session peak |
| `STOP_ON_CONSECUTIVE_LOSSES = 0` | disabled |

### Drawdown Protection (Recovery mode)

A soft mechanism that **does not stop** the bot but caps recovery bets when balance drops too far from its peak.

```
Threshold = INITIAL_BALANCE × DRAWDOWN_PROTECTION_PCT / 100
          = 30,000 × 10% = 3,000 KES
```

- **Activates** when `peak_pnl − current_pnl ≥ 3,000 KES`
- **Deactivates** when drawdown recovers below 50% of threshold (1,500 KES)
- While active: effective deficit fed into bet formula is capped at 3,000 KES
- Max P1 bet in protection mode: `(3,000 + 50 + 25) / 1.5 = 2,050 KES`
- Max P1 bet normal: `(5,000 + 50 + 25) / 1.5 = 3,383 KES`

This prevents exponential bet escalation during a bad run. The floor trails the session peak upward — gains are locked in.

```python
DRAWDOWN_PROTECTION_PCT = 10.0   # 0 = disabled
INITIAL_BALANCE         = 30000  # used to compute threshold (real bankroll)
```

### Profit Protection (after target reached)

Once `STOP_ON_PROFIT` is reached, a secondary guard prevents giving back a large fraction of peak profit in a single round:

```
STOP_PROFIT_LOSS_FRAC     = 0.25   # stop if one round loses > 25% of peak
STOP_PROFIT_LOSS_FRAC_MAX = 0.50   # scales to 50% as profit grows to 2× target
```

Example: peak = 4,000 KES at profit target → stop if any single round loses > 1,000 KES.

### Auto-Restart

```python
AUTO_RESTART_SESSION = True   # start a new session after each stop
RESTART_DELAY        = 10     # seconds between sessions
```

Each new session resets PnL and deficits. Lifetime PnL across all sessions is tracked separately in logs.

---

## Quick-Reference: Current Config

```
Mode            : Recovery (AM_STRATEGY_ENABLED = False)
P1 trigger      : prev crash > 2.5x  →  cashout 2.5x
P2 trigger      : 1.4x < prev crash < 3.5x  →  cashout 3.5x
Recovery scope  : smart (P1 covers both; P2 suppressed while P1 leads)
Chunk cap       : 5,000 KES (10% of 50k demo balance)
Drawdown protect: 3,000 KES threshold (10% of 30k real bankroll)
Stop on profit  : +3,000 KES
Stop on drawdown: 20% from peak
Base bets       : P1 = 50 KES  |  P2 = 50 KES
Profit target   : 25 KES per P1 win  |  25 KES per P2 win
```

To switch to AM mode:
```
AM_STRATEGY_ENABLED = True
AM_TRIGGER_CRASH    = 8.0
AM_CASHOUT          = 7.0
AM_BET_AMOUNT       = 50.0
AM_MAX_STREAK       = 4
```
