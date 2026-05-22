# Aviator Bot — Strategy Documentation

**Branch:** `prod_v2` / `prod_v2_1`
**Last updated:** 2026-05-22
**Backtest data:** 11,339 rounds (May 19–22 2026) + 49 real sessions from logs

---

## Overview

Two independent betting panels watch crash history and fire independently based on separate trigger conditions. Panel 1 (P1) is the primary recovery engine — it scales its bet size to cover the combined deficit of both panels. Panel 2 (P2) is the secondary panel, always betting at base amount.

### Core cashout targets

| Panel | Cashout | Net multiplier | Win rate (historical) |
|---|---|---|---|
| P1 | 2.5x | 1.5x | 38.7% |
| P2 | 3.5x | 2.5x | 27.6% |
| P1 (assist mode) | 1.4x | 0.4x | 69.7% |

---

## Trigger Conditions

### Panel 1 — THREE possible triggers (checked in priority order)

**1. P1 ASSIST** (highest priority)
- Fires when: previous crash `<= 1.4x` AND P2 has an uncleared deficit
- Intent: the game just crashed very low, a rebound above 1.4x is likely (69.7% historical hit rate), use it to start eating away at P2's debt
- P1 temporarily uses a 1.4x cashout for this round only, then reverts

**2. P1 HIGH**
- Fires when: previous crash `> 2.5x`
- Intent: after a high crash the next round statistically tends to be closer to the median (1.93x) — but this is NOT a strong mean-reversion signal; 38.7% of all rounds still land below 2.5x
- Blocked when combined deficit >= `RECOVERY_DEFICIT_CAP` (guardrail)

**3. P1 LOW STREAK** *(currently disabled)*
- Would fire after N consecutive crashes all `<= LOW_STREAK_MAX`
- Disabled in prod_v2: `P1_LOW_STREAK_MAX = 0.0`

### Panel 2 — ONE trigger

**P2 LOW**
- Fires when: previous crash is in range `(1.4x, 3.5x)` exclusive
- Intent: crash was moderate, next round likely also moderate — bet P2's 3.5x cashout
- Historical coverage: 41.6% of all rounds fall in this zone

### Trigger frequency (historical)

| Trigger | Count / 11,339 rounds | % of rounds |
|---|---|---|
| P1 HIGH | 4,365 | 38.5% |
| P2 LOW | 4,722 | 41.6% |
| P1 ASSIST | 1,909 | 16.8% |

Both P1 and P2 can trigger on the same round (their conditions are independent). When both are active and P1 is in recovery mode, P2 is "suppressed" — it bets base amount and its win/loss does not affect the deficit tracker.

---

## Bet Sizing

### Panel 2
Always bets `P2_BET_AMOUNT = 50 KES`. Never scales.

### Panel 1 — Normal recovery mode (`RECOVERY_SCOPE = "smart"`)

```
target   = P1_deficit + P2_deficit          (P1 covers both panels)
net_mult = PANEL1_CASHOUT - 1 = 1.5
P1_bet   = max(BET_AMOUNT, (target + RECOVERY_PROFIT_TARGET) / net_mult)
         = max(50, (deficit + 25) / 1.5)
```

Example:
| Combined deficit | P1 bet (uncapped) | P1 bet (with MAX_RECOVERY_BET=500) |
|---|---|---|
| 0 KES | 50 KES | 50 KES |
| 100 KES | 83.33 KES | 83.33 KES |
| 500 KES | 350 KES | 350 KES |
| 700 KES | 483.33 KES | 483.33 KES |
| 750 KES | 516.67 KES | **500 KES** (capped) |
| 5,000 KES | 3,350 KES | **500 KES** (capped) |

### Panel 1 — Assist mode (`P1_ASSIST_CASHOUT = 1.4x`)

```
target   = P2_deficit × P1_ASSIST_PERCENTAGE / 100   (= 100% of P2 deficit)
net_mult = P1_ASSIST_CASHOUT - 1 = 0.4
P1_bet   = max(BET_AMOUNT, (target + RECOVERY_PROFIT_TARGET) / net_mult)
         = max(50, (P2_deficit + 25) / 0.4)
```

This formula is aggressive because the 0.4 net multiplier requires a large stake. The `MAX_ASSIST_BET = 300 KES` cap prevents this from spiraling.

---

## Recovery Logic

**On P1 WIN** (crash >= P1 cashout for that round):
- Both `P1_deficit` and `P2_deficit` are cleared to 0
- P1 reverts to base bet (50 KES)
- Burst cooldown applied before next trigger can fire

**On P1 LOSS** (crash < P1 cashout):
- `P1_deficit += P1_bet_used`
- Pattern completes → back to watch mode
- Cooldown = `BURST_COOLDOWN + TRIGGER_LOSS_COOLDOWN` (if it was a HIGH trigger loss)

**On P2 WIN** (crash >= 3.5x, when P1 is not leading recovery):
- Both `P1_deficit` and `P2_deficit` are cleared to 0 (RECOVERY_SCOPE = "combined")

**On P2 LOSS** (crash < 3.5x):
- If P1 was leading recovery (suppressed): P2 loss does NOT add to deficit
- Otherwise: `P2_deficit += P2_bet_used`

---

## Guardrails (added 2026-05-22 after backtest)

Backtest without guardrails showed: max 16 consecutive P1 trigger losses → single bet of KES 106,000 and capital at risk of KES 159,000. These 4 parameters prevent that.

| Parameter | Value | Purpose |
|---|---|---|
| `MAX_RECOVERY_BET` | 500 KES | Hard cap on any single P1 recovery bet |
| `MAX_ASSIST_BET` | 300 KES | Hard cap on P1 assist-mode bet (it uses 1.4x cashout = high risk) |
| `MAX_P2_BET` | 200 KES | Hard cap on any single P2 recovery bet — **added after 2026-05-22 blowup** |
| `RECOVERY_DEFICIT_CAP` | 2,000 KES | Blocks new P1 HIGH triggers once combined deficit reaches this; existing burst still completes; P1 ASSIST can still fire |
| `TRIGGER_LOSS_COOLDOWN` | 2 rounds | Extra watch rounds added after each P1 HIGH trigger loss, on top of `BURST_COOLDOWN` |

### Before vs after guardrails (simulation on 11,339 rounds)

| Metric | Without guardrails | With guardrails |
|---|---|---|
| Worst P&L reached | KES -49,453 | KES -18,754 (-62%) |
| Max single bet | KES 7,134 | KES 500 (hard cap) |
| Max combined deficit | KES 18,049 | KES 3,045 (-83%) |
| Rounds deficit > 500 KES | 1,241 | 960 (-23%) |

**Trade-off:** Recovery is now gradual — each P1 win recovers the capped amount, not the full deficit in one shot. Upside ceiling per session is lower, but catastrophic blowouts are prevented.

---

## Incident Log

### 2026-05-22 21:37–21:55 — Account blowup (KES -54,481)

**What happened:** P2 blew the account, not P1. Root cause chain:
1. P1 took losses → P1 deficit reached KES 3,437
2. P2 trigger fired (crash was in 1.4–3.5x zone) while P1 was watching
3. `P2_RECOVERY_SCOPE = "combined"` caused P2 to size its bet using P1's deficit: `(3437 + 25) / 2.5 = KES 1,385`
4. P2 has a 27.6% win rate — it lost, deficit grew to KES 3,324
5. Next round P2 bet KES 1,939 → lost → KES 2,714 → lost → escalated to KES 11,977 in 8 minutes
6. No `MAX_P2_BET` cap existed — P2 was completely uncapped

**Contributing factors:**
- First guardrails commit only capped P1 bets — P2 was missed entirely
- Session was already running on old code when guardrails were committed

**Fixes applied:**
- `P2_RECOVERY_SCOPE` changed from `"combined"` → `"individual"` (P2 only targets its own deficit, not P1's)
- `MAX_P2_BET = 200` KES added to `calc_p2_bet()` in both bot files
- Verified: in the blowup scenario (P1_def=3437, P2_def=0), old code produced P2 bet of KES 1,385; new code produces KES 50 (base bet, correct)

---

## Historical Crash Distribution (11,339 rounds)

| Range | Count | % | Notes |
|---|---|---|---|
| < 1.5x | 3,949 | 34.8% | All bets lose |
| 1.5–2.5x | 3,004 | 26.5% | P2 trigger zone but below P1 cashout |
| 2.5–3.5x | 1,261 | 11.1% | P1 wins, P2 loses |
| 3.5–5x | 887 | 7.8% | Both panels win |
| 5–10x | 1,103 | 9.7% | Both win; P1 HIGH triggers next round |
| 10–20x | 590 | 5.2% | Both win; P1 HIGH triggers next round |
| > 20x | 545 | 4.8% | Both win; P1 HIGH triggers next round |

- Mean crash: **7.13x** — pulled up by rare spikes (max 948.96x)
- Median crash: **1.93x** — the "typical" round

**Dangerous sequences from history:**
- Max consecutive rounds below P1 cashout (2.5x): **18 rounds**
- Max consecutive rounds below P2 cashout (3.5x): **26 rounds**
- Average rounds to clear a deficit: **4.4** — Max ever: **33 rounds**

---

## Real Session Performance (49 sessions from logs)

| Metric | Value |
|---|---|
| Profitable sessions | 30 / 49 (61%) |
| Best session | +35,750 KES |
| Worst session | -51,058 KES |
| Average session P&L | -118 KES |
| Net across all sessions | -5,796 KES |
| Largest P1 bet placed live | 50,404 KES |

> Note: live sessions start with zero deficit each time. The continuous simulation is more conservative than reality — it accumulates deficit across what would be separate sessions.

---

## Configuration Reference (current prod_v2_1)

```python
BET_AMOUNT            = 50       # KES base bet (both panels)
PANEL1_CASHOUT        = 2.5
PANEL2_CASHOUT        = 3.5
P1_TRIGGER_MULT       = 2.5      # P1 fires when previous crash > this
P2_LOW_STREAK_MIN     = 1.4      # P2 fires when previous crash is in (1.4, 3.5)
P2_LOW_STREAK_MAX     = 3.5
P1_ASSIST_TRIGGER_MAX = 1.4      # P1 assist fires when crash <= this AND P2 in deficit
P1_ASSIST_CASHOUT     = 1.4
RECOVERY_SCOPE        = "smart"  # P1 covers combined deficit; P1 win clears both
RECOVERY_PROFIT_TARGET = 25      # KES profit margin built into P1 bet formula
BURST_COOLDOWN        = 0
# Guardrails
MAX_RECOVERY_BET      = 500      # KES
MAX_ASSIST_BET        = 300      # KES
RECOVERY_DEFICIT_CAP  = 2000     # KES
TRIGGER_LOSS_COOLDOWN = 2        # rounds
```

---

## Ideas for Future Improvement

These are hypotheses to test with more data — do not implement without running `backtest.py` first.

### 1. Smarter P1 trigger filter
Currently P1 HIGH fires after every crash > 2.5x. But a high crash following another high crash is different from a high crash breaking a low streak. Consider:
- Only fire P1 HIGH if the previous N rounds averaged below some threshold (confirms a true rebound setup)
- Add `P1_TRIGGER_MULT_MAX` to ignore very extreme crashes (> 50x) — they may signal unusual game conditions

### 2. Time-of-day weighting
The game's crash distribution may differ by time. Collect timestamp data and compare crash distributions morning vs evening vs night. Adjust trigger thresholds per time window.

### 3. Deficit-aware cashout target
When in heavy deficit, temporarily lower P1 cashout from 2.5x to 2.0x — the win rate improves from 38.7% to ~50%, accepting a smaller net gain per win in exchange for faster recovery frequency.

### 4. Partial deficit recovery (gradual spread)
Instead of "clear all deficit on a single P1 win", target recovering only 50% per win:
- `RECOVERY_SCOPE = "percentage"`, `RECOVERY_PERCENTAGE = 50`, `RECOVERY_STEPS = 3`
- Smaller bet per round, slower recovery, but more wins needed means more rounds of exposure

### 5. Adaptive cooldown
Instead of a fixed `TRIGGER_LOSS_COOLDOWN`, make cooldown scale with the current deficit:
- Deficit < 500: cooldown = 1
- Deficit 500–1000: cooldown = 3
- Deficit > 1000: cooldown = 5
Prevents re-entry when the market is clearly in a hostile low-crash streak.

### 6. P2 multi-round pattern
Currently P2 bets only 1 round per trigger (`P2_BET_PATTERN = [1]`). Test a 2-round pattern: the first round bets after the trigger, the second round bets again if P2 lost — doubling down once. More wins in a single session at the cost of larger P2 losses.

### 7. Minimum balance guard
Add a check: if `STOP_ON_LOSS` is close (within 20%), tighten `MAX_RECOVERY_BET` and `MAX_ASSIST_BET` automatically to preserve remaining capital.

---

## How to Re-run the Backtest

```bash
source .venv/bin/activate
python backtest.py
# reads history/*.csv + all *.log files
# outputs to stdout and backtest_report.txt
```

To test a config change before applying it to the live bot, edit the parameter block at the top of `backtest.py` and re-run. Compare the key metrics: `worst_pnl`, `max_deficit`, `max_single_bet`, and `rounds_deficit_gt_500`.
