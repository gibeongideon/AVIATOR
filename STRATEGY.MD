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
