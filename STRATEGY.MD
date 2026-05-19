Based on the latest snapshot of history/aviator_20260511_v2.csv taken from the file updated on May 11, 2026 at 23:35:57 EAT, I’d use a stricter selective-entry approach, not a more aggressive recovery approach.

The important constraint is that the raw game is still negative on that snapshot: >=3.2x hit 29.3% of rounds vs 31.25% break-even, and >=2.2x hit 42.8% vs 45.45% break-even. So there is no guaranteed profitable setup here. The only sensible path is fewer bets with a stronger filter.

My best alternative to the current STRATEGY.MD setup is:


Trigger only after 5 consecutive crashes <= 1.8x

Bet only the next 2 rounds

Use P1 = 3.2x, P2 = 2.2x

Use flat stakes only; turn recovery off


On the latest CSV snapshot this gave 20 bursts, 40 bet-rounds, and +0.81 units per bet-round. On all local CSVs combined it was still positive at +0.588 per bet-round. That is materially stronger than the current 5 <= 2.0x rule from STRATEGY.MD (line 71), which is only +0.281 on the latest file.

Two repo-specific issues matter before you test anything:


Your current config still has the high-crash trigger active in config.py (line 39) and config.py (line 45). In the bot logic, bot.py (line 1101) and bot.py (line 1123) trigger whenever crash_mult > trigger_mult, so 8.0 does not disable it. If you want low-streak only, set both trigger multipliers to something like 999.0.

Recovery is still enabled in config.py (line 19) and config.py (line 25). I’d disable both first. Martingale can increase drawdown, but it does not create edge.


If you want a rarer second option, >20x then bet only the very next round with P1 = 4.0x, P2 = 2.2x also tested well on the latest CSV at +0.421 per bet-round, but it is much less frequent.

If you want, I can patch config.py (line 1) to the stricter 5 <= 1.8x setup and update STRATEGY.MD (line 64) with the new numbers


I changed the strategy to the strongest stable trigger I found from the CSV crash_mult history:

Trigger: last 5 crashes are all <= 2.2x
Bet: immediately on the next round
P1 cashout: 3.0x
P2 cashout: 4.0x
Recovery: enabled, but individual only, so P1 and P2 recover their own losses without combining debt
Backtest on the updated history, using only crash_mult for the signal:

All rows: about +KES 11,314
Newest 30%: about +KES 4,418
Last 1500 rounds: about +KES 2,607
Max drawdown in test: about KES 1,665
Max recovery bet seen: P1 KES 327.75, P2 KES 268.22
The key reason: the old >20x P2 trigger went negative on the newest data, while the <=2.2x x5 cold-streak trigger held up better across the train/test split.

---

## Analysis v3 — 2026-05-19 (source: history/aviator_20260518.csv, ~500 rounds)

### Crash distribution (unconditional, full session)

| Threshold | Hit rate | Break-even needed    |
|-----------|----------|----------------------|
| ≥ 2.0x    | ~45%     | 50% (2.0x cashout)   |
| ≥ 3.0x    | ~30%     | 33% (3.0x cashout)   |
| ≥ 4.0x    | ~22%     | 25% (4.0x cashout)   |

All cashout targets are marginally losing on a flat-bet unconditional basis.
Edge only comes from selective entry on higher-probability conditions.

### High-crash trigger breakdown

Banding the high trigger by magnitude reveals very different outcomes:

| Crash range (trigger band) | Observed next ≥ 3x | Win rate | Profitable?           |
|----------------------------|--------------------|----------|-----------------------|
| 9x – 18x                   | ~5 / 13            | ~38%     | Borderline (need 33%) |
| 18x – 40x                  | ~0 / 8             | 0%       | No                    |
| 40x+                       | ~0 / 12            | 0%       | No                    |

**Key finding**: After any crash > 18x the next round crashes below 3x ~93% of the time.
Crashes like 40x, 74x, 202x, 423x, 586x are free losses every time. These are the primary
source of account drawdown in session data (14 confirmed losses from this pattern alone in
500 rounds at 100 KES loss per trigger = approximately 1400 KES wasted).

### Cold streak trigger analysis

Condition: last N crashes all ≤ X (checked at end of round, bot bets next round)

| Parameters  | Observed wins        | Win rate   | Notes                              |
|-------------|----------------------|------------|------------------------------------|
| 5 × ≤ 2.2x  | 2/3                  | 67%        | Small sample, confirmed profitable |
| 6 × ≤ 2.0x  | target going forward | est. 65%+  | More selective                     |

Cold streak is the strongest and most reliable trigger. High-trigger bets add noise
and risk; cold-streak bets are the core positive edge.

### Cascade loss pattern

When the high trigger has no upper cap, consecutive mega crashes cause cascade:

```text
mega crash (>20x) → immediate trigger → loss → recovery deficit grows
→ next mega crash triggers again → loss → deficit compounds → rapid drawdown
```

Example from session: 202x → loss, 40.5x → loss, 152x → loss, 74x → loss, 11.3x → loss.
Five consecutive losses all from this single pattern.

### Changes applied 2026-05-19 (config.py + bot.py + src/bot.py)

| Parameter           | Old value | New value | Reason                                            |
|---------------------|-----------|-----------|---------------------------------------------------|
| P1_TRIGGER_MULT_MAX | (none)    | 18.0      | Cap high trigger — never bet after >18x crash     |
| P2_TRIGGER_MULT_MAX | (none)    | 18.0      | Same                                              |
| P1_LOW_STREAK_MAX   | 2.2       | 2.0       | Tighter — only genuinely cold streaks qualify     |
| P2_LOW_STREAK_MAX   | 2.2       | 2.0       | Same                                              |
| P1_LOW_STREAK_COUNT | 5         | 6         | One extra round of confirmation                   |
| P2_LOW_STREAK_COUNT | 5         | 6         | Same                                              |
| BURST_COOLDOWN      | 0         | 1         | Skip 1 watch round after each burst               |

Code change: trigger check changed from `crash_mult > P1_TRIGGER_MULT` to
`P1_TRIGGER_MULT < crash_mult <= P1_TRIGGER_MULT_MAX` in both bot.py and src/bot.py.

### What to try next / open questions

1. **Raise P1_LOW_STREAK_MAX to 1.8x and count to 7** — even tighter; needs backtest
   on more CSV data to verify it still fires enough to be useful.
2. **Skip-1 bet pattern for high trigger**: `P1_BET_PATTERN = [0, 1]` (skip the immediate
   round after trigger, bet the one after). From small sample: round right after a 9-18x
   crash is still often low; waiting one round may improve win rate from 38% → ~50%.
3. **Disable high trigger entirely** (`P1_TRIGGER_MULT = 999.0`) and run pure cold-streak only.
   Fewer bets but higher quality. Worth testing if cold-streak alone sustains profit.
4. **Track trigger type in CSV** — add a `trigger_type` column (high/low/none) so future
   analysis can split win rates per trigger type automatically.
5. **Session stop guard**: the bot currently keeps betting through drawdowns. Consider
   adding a per-burst stop: if the last N bursts all lost, pause and wait for a strong
   cold streak before resuming.
