# Aviator Strategy Notes

## Active Strategy - 2026-05-20

Source data:

- All local CSV crash history in `history/*.csv`
- Newest added history: `history/aviator_20260520.csv`
- Total usable rows: 16,647 crash rounds
- Newest holdout rows: 999 crash rounds from 2026-05-20

The active `config.py` strategy is a selective cold-streak plan:

| Setting | Value |
|---------|-------|
| P1 cashout | `2.2x` |
| P2 cashout | `3.0x` |
| P1 stake | `50 KES` |
| P2 stake | `50 KES` |
| Recovery | Off |
| High-crash trigger | Disabled |
| Low-streak trigger | Last `8` crashes all `<= 2.5x` |
| Bet pattern | `[0, 1]` - skip one round, then bet once |
| Cooldown | `1` watch round after each burst |

Current config values:

```python
PANEL1_CASHOUT = 2.2
PANEL2_CASHOUT = 3.0

RECOVERY_ENABLED = False
P2_RECOVERY_ENABLED = False
P1_ASSIST_P2_ENABLED = False
P2_ASSIST_P1_ENABLED = False

P1_TRIGGER_MULT = 999.0
P1_TRIGGER_MULT_MAX = 999.0
P1_LOW_STREAK_MAX = 2.5
P1_LOW_STREAK_COUNT = 8
P1_BET_PATTERN = [0, 1]
P1_MAX_BET_ROUNDS = 1

P2_TRIGGER_MULT = 999.0
P2_TRIGGER_MULT_MAX = 999.0
P2_LOW_STREAK_MAX = 2.5
P2_LOW_STREAK_COUNT = 8
P2_BET_PATTERN = [0, 1]
P2_MAX_BET_ROUNDS = 1
```

## Backtest Result

Backtest was reconstructed from crash history only, using the bot's trigger timing:

1. Observe crash round.
2. If trigger fires, load the pattern.
3. For `[0, 1]`, skip the next round and bet the round after that.
4. Score P1 and P2 independently against their cashout targets.

| Dataset | PnL | Bet rounds | Panel bets | Bursts | Max drawdown | Round wins/losses |
|---------|-----|------------|------------|--------|--------------|-------------------|
| Older history | `+KES 1,950` | `150` | `300` | `150` | `KES 1,020` | `75 / 75` |
| New 20260520 history | `+KES 570` | `11` | `22` | `11` | `KES 200` | `7 / 4` |
| All history | `+KES 2,520` | `161` | `322` | `161` | `KES 1,020` | `82 / 79` |

Profit per panel bet:

- Older history: `+KES 6.50`
- New 20260520 history: `+KES 25.91`
- All history: `+KES 7.83`

## Why This Replaced The Previous Setup

The previous active setup was:

```python
PANEL1_CASHOUT = 3.0
PANEL2_CASHOUT = 4.0
RECOVERY_ENABLED = True
P2_RECOVERY_ENABLED = True
P1_TRIGGER_MULT = 9.0
P1_TRIGGER_MULT_MAX = 18.0
P1_LOW_STREAK_MAX = 2.0
P1_LOW_STREAK_COUNT = 6
P1_BET_PATTERN = [1]
```

Flat backtest of that trigger/cashout shape was negative:

| Dataset | PnL | Bursts | Main issue |
|---------|-----|--------|------------|
| Older history | `-KES 6,350` | `961` | Too many high-trigger entries |
| New 20260520 history | `-KES 650` | `66` | 49 high-trigger bursts vs 17 low-streak bursts |
| All history | `-KES 6,750` | `1,027` | High-trigger leak dominates |

The recovery version was worse on full history because recovery increases exposure during bad stretches:

| Dataset | PnL | Max drawdown | Max P1 bet seen |
|---------|-----|--------------|-----------------|
| Older history | `-KES 17,477.10` | `KES 41,586.22` | `KES 10,346.86` |
| New 20260520 history | `-KES 322.38` | `KES 822.38` | `KES 138.27` |
| All history | `-KES 17,442.08` | `KES 41,586.22` | `KES 10,346.86` |

Conclusion: recovery can sometimes rescue a small losing session, but it creates unacceptable tail risk. It does not create edge; it only moves losses into larger future bets.

## Robustness Check

The highest total-profit candidate in the sweep was:

```text
Low streak: 8 crashes <= 2.5x
Pattern: [0, 1]
P1/P2 cashout: 3.0x / 3.0x
Recovery: off
```

That produced `+KES 3,100` on all history and `+KES 700` on the new 20260520 file. However, it had a losing individual day on `history/aviator_20260512_v2.csv`.

The active `2.2x / 3.0x` version was chosen because it was more stable by file:

| File | Active strategy result |
|------|------------------------|
| `history/aviator_20260510.csv` | `+KES 150` |
| `history/aviator_20260511.csv` | `KES 0` |
| `history/aviator_20260511_v2.csv` | `+KES 740` |
| `history/aviator_20260512_v2.csv` | `+KES 40` |
| `history/aviator_20260513.csv` | `+KES 280` |
| `history/aviator_20260514.csv` | `+KES 130` |
| `history/aviator_20260515.csv` | `KES 0` |
| `history/aviator_20260518.csv` | `+KES 180` |
| `history/aviator_20260519.csv` | `+KES 430` |
| `history/aviator_20260520.csv` | `+KES 570` |

This is the main reason for using P1 `2.2x` instead of making both panels chase `3.0x`.

## Operating Guidance

- Keep recovery off unless a new backtest proves it improves drawdown, not only profit.
- Keep high-crash triggers disabled. In the current history, the high-trigger band produces too many low-quality entries.
- Treat the strategy as selective. It may sit idle for long stretches.
- Do not raise stake size just because the trigger is rarer; the edge estimate is still based on limited local history.
- Re-test after each new day of history before changing cashouts or low-streak count.

## Caveat

Aviator crash outcomes are still gambling outcomes. Backtests can overfit, and a positive historical filter is not a guarantee of future profit. The current plan is chosen for lower exposure and better historical robustness, not certainty.




TESTED STRATEGIES
Best progressive systems on top conditions

System	prev≥8x @ 7x	prev≥8x @ 8x	Worst bal
Anti-Martingale ×4	+37,500	+40,650	~44,300
Paroli ×3	+31,350	+35,100	~44,450
1-3-2-6	+30,850	+31,450	~44,200
Flat	+20,100	+21,700	~45,100
Oscar's Grind	-36,517	+61,782	-15,928
Martingale	-182,100	-261,300	-193,450
Fibonacci	-483,550	-737,150	-458,550
