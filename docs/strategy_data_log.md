# Strategy Data Log

Living notes for strategy changes made from Aviator crash-history CSVs.

This document should be updated every time new `history/*.csv` data is reviewed. Use only the `crash_mult` column for pattern analysis unless a future entry explicitly says otherwise.

## Current Live Strategy

Updated: 2026-05-13

Source files reviewed:

- `history/aviator_20260510.csv`
- `history/aviator_20260511.csv`
- `history/aviator_20260511_v2.csv`
- `history/aviator_20260512_v2.csv`

Rows analyzed: about 7,900 crash multipliers.

### Config Change

The previous setup was drifting negative on the newest data, mainly because the P2 `>20x` trigger weakened sharply out of sample.

Current `config.py` strategy:

| Panel | Trigger | Pattern | Cashout | Recovery |
| --- | --- | --- | --- | --- |
| P1 | Last 5 crashes all `<= 2.2x` | Bet next round | `3.0x` | Enabled, individual; assists `50%` of P2 deficit when P1 has no deficit |
| P2 | Last 5 crashes all `<= 2.2x` | Bet next round | `4.0x` | Enabled, individual |

High-crash triggers are disabled by setting:

```python
P1_TRIGGER_MULT = 9999.0
P2_TRIGGER_MULT = 9999.0
```

### Data Finding

The strongest repeatable trigger in the current CSV set was a cold-streak setup:

```text
last 5 crash_mult values <= 2.2
```

After this pattern, the next round performed better than the broad baseline at `3.0x` and `4.0x` targets. The older P2 rule, `crash_mult > 20` then bet next round at `4.0x`, looked positive on older data but was negative on the newest 30% and on the latest 1500 rounds.

### Backtest Snapshot

Backtest assumptions:

- Signal uses `crash_mult` only.
- Trigger is checked after a round ends.
- Bets are placed on the next betting window.
- P1 and P2 recovery are independent.
- Base bet is `KES 50` per panel.

| Window | Result | Notes |
| --- | ---: | --- |
| All available rows | about `+KES 11,314` | 810 total panel bets |
| First 70% | about `+KES 6,717` | Training window |
| Newest 30% | about `+KES 4,418` | Out-of-sample check |
| Last 1500 rounds | about `+KES 2,607` | Recent-performance check |

Risk observed in the same backtest:

| Metric | Value |
| --- | ---: |
| Max drawdown | about `KES 1,665` |
| Max P1 recovery bet | about `KES 327.75` |
| Max P2 recovery bet | about `KES 268.22` |

### Decision

Use the cold-streak trigger on both panels, with different cashouts, because it was more stable than the high-crash trigger and stayed positive on the newest split.

Keep recovery `individual`, not `combined`, because combined recovery produced larger profit in the historical sample but also much larger bet spikes. The current goal is smoother recovery, not maximum historical profit.

## Update Procedure

When new CSVs are added:

1. Load all `history/*.csv` files.
2. Deduplicate exact duplicate `timestamp + crash_mult` rows.
3. Ignore all columns except `crash_mult`.
4. Recalculate broad baseline hit rates for common cashouts: `2.0x`, `3.0x`, `3.2x`, `4.0x`, `5.0x`, `10.0x`, `20.0x`.
5. Retest the current strategy on:
   - all rows
   - first 70%
   - newest 30%
   - last 1500 rounds
   - each calendar day
6. Compare against candidate triggers:
   - low streaks: `<= 1.5x`, `<= 1.8x`, `<= 2.0x`, `<= 2.2x`, `<= 2.5x`
   - streak lengths: `3`, `4`, `5`, `6`, `7`
   - high triggers: `> 10x`, `> 20x`, `> 30x`, `> 50x`
   - patterns: `[1]`, `[0, 1]`, `[1, 1]`
7. Prefer a candidate only if it improves the newest 30% without creating unacceptable max bet spikes or drawdown.
8. Record the decision in the changelog below.

## Changelog Template

Copy this section for each future review.

```markdown
## YYYY-MM-DD Review

Source files reviewed:

- `history/...`

Rows analyzed:

Current config tested:

| Window | PnL | Bets | ROI | Max drawdown | Max bet |
| --- | ---: | ---: | ---: | ---: | ---: |
| All rows |  |  |  |  |  |
| First 70% |  |  |  |  |  |
| Newest 30% |  |  |  |  |  |
| Last 1500 |  |  |  |  |  |

Best candidate tested:

- Trigger:
- Pattern:
- P1 cashout:
- P2 cashout:
- Recovery:

Decision:

- Keep current strategy / change config.

Reason:

- 

Config changes:

- 

Risk notes:

- 
```

## Change History

### 2026-05-13 Review

Changed `config.py` from:

- P1: `<= 1.8x` for 5 rounds, skip one round, then bet at `3.2x`
- P2: `> 20x`, bet next round at `4.0x`
- Recovery disabled

To:

- P1: `<= 2.2x` for 5 rounds, bet next round at `3.0x`
- P2: `<= 2.2x` for 5 rounds, bet next round at `4.0x`
- Recovery enabled as `individual` on both panels
- P1 assist enabled: when P1 has no deficit, it targets `50%` of P2's deficit

Reason:

- Old combined setup was positive on all rows but negative on the newest 30%.
- The `>20x` P2 trigger was the weakest recent component.
- The `<=2.2x` for 5 rounds trigger was positive in all calendar-day checks and in the newest split.

Risk note:

- Historical positive performance does not prove a real edge. Run in demo first after each config change and re-review after new CSVs are collected.
