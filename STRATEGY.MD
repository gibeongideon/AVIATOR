# Aviator Strategy Notes

This file reflects the active strategy settings in `config.py`.

## Active Mode

The bot is currently configured for **Recovery mode**, not Anti-Martingale.

```python
AM_STRATEGY_ENABLED = False
```

That means the bot uses the two Aviator betting panels as independent trigger
lanes, tracks losing bets as recovery deficits, and sizes future bets to recover
those deficits plus a profit target.

## Active Panel Setup

| Setting | Current value |
|---|---:|
| Panel 1 base bet | `50 KES` |
| Panel 2 base bet | `50 KES` |
| Panel 1 cashout | `2.5x` |
| Panel 2 cashout | `3.5x` |
| Demo mode | `True` |
| Headless browser | `True` |
| Initial real bankroll reference | `30,000 KES` |
| Initial demo bankroll reference | `50,000 KES` |

## Active Trigger Logic

The bot only evaluates triggers after a round ends. The just-finished crash
multiplier becomes the signal for the next round.

### Global Gate

```python
MIN_TRIGGER_CRASH = 1.22
```

If the previous crash is below `1.22x`, all P1/P2 triggers are skipped for the
next round.

### Panel 1 Trigger

```python
P1_TRIGGER_MULT     = 2.5
P1_TRIGGER_MULT_MAX = float("inf")
P1_LOW_STREAK_MAX   = 0.0
P1_LOW_STREAK_COUNT = 1
P1_BET_PATTERN      = [1]
P1_MAX_BET_ROUNDS   = 1
```

P1 currently triggers when:

```text
previous crash > 2.5x
```

The low-streak trigger is effectively disabled because `P1_LOW_STREAK_MAX = 0.0`
and crash values are positive.

When P1 triggers, it bets the next available round once.

### Panel 2 Trigger

```python
P2_LOW_STREAK_MIN   = 1.4
P2_LOW_STREAK_MAX   = 3.5
P2_LOW_STREAK_COUNT = 1
P2_BET_PATTERN      = [1]
P2_MAX_BET_ROUNDS   = 1
```

P2 currently triggers when:

```text
1.4x < previous crash < 3.5x
```

When P2 triggers, it bets the next available round once.

## Active Recovery Strategy

Recovery is enabled for both panels.

```python
RECOVERY_ENABLED    = True
P2_RECOVERY_ENABLED = True
```

### Panel 1 Recovery

```python
RECOVERY_SCOPE         = "smart"
RECOVERY_PROFIT_TARGET = 25
RECOVERY_PERCENTAGE    = 50
RECOVERY_STEPS         = 2
```

P1 is the priority recovery lane. In `smart` scope, P1 targets the combined P1
and P2 deficit. If P1 wins while leading recovery, both deficits can be cleared.

P1 recovery bet sizing:

```text
P1 bet = (target deficit + extra risk + 25) / (2.5 - 1)
```

### Panel 2 Recovery

```python
P2_RECOVERY_SCOPE         = "combined"
P2_RECOVERY_PROFIT_TARGET = 25
P2_RECOVERY_PERCENTAGE    = 100
P2_RECOVERY_STEPS         = 2
```

P2 can also recover combined deficit, but when P1 is already leading recovery,
P2 recovery is suppressed and P2 uses its base bet instead.

P2 recovery bet sizing:

```text
P2 bet = (target deficit + extra risk + 25) / (3.5 - 1)
```

## Bet Cap And Risk Controls

### Recovery Chunk Cap

```python
RECOVERY_CHUNK_CAP     = 0
RECOVERY_CHUNK_CAP_PCT = 10
INITIAL_DEMO_BALANCE   = 50000
```

Because demo mode is active, the effective recovery chunk cap is:

```text
10% of 50,000 KES = 5,000 KES
```

This prevents one recovery attempt from targeting more than `5,000 KES` of
deficit at once.

### Session Stops

| Guard | Current value |
|---|---:|
| Stop on profit | `3,000 KES` |
| Stop on loss | Disabled |
| Stop on drawdown from peak PnL | `20%` |
| Drawdown protection | `10%` |
| Stop on consecutive losses | Disabled |
| Burst cooldown | `0` rounds |
| Auto restart | `True` |
| Restart delay | `10` seconds |

## Disabled Optional Behaviors

These strategy helpers exist in code but are currently off:

| Feature | Current value | Effect when enabled |
|---|---:|---|
| P1 assist P2 | `False` | P1 helps clear P2 deficit at low cashout |
| P2 assist P1 | `False` | P2 helps clear P1 deficit |
| P1 low-zone recovery | `False` | P1 chips away at deficit after low crashes |
| P1 follows P2 | `False` | P1 base-bets when only P2 triggers |
| P2 follows P1 | `False` | P2 base-bets when only P1 triggers |

## Predictor Gate

The ML predictor is enabled.

```python
PREDICTOR_ENABLED        = True
PREDICTOR_MIN_ROUNDS     = 1000
PREDICTOR_RETRAIN_ROUNDS = 500
PREDICTOR_P1_CONFIDENCE  = 1.0
PREDICTOR_P2_CONFIDENCE  = 1.0
```

In Recovery mode, P1/P2 triggers can be blocked if the model estimates that the
chance of reaching the panel cashout is below break-even:

```text
minimum probability = confidence / cashout
```

With confidence set to `1.0`, the bot requires at least break-even probability.

## Available But Inactive: Anti-Martingale

Anti-Martingale is configured but inactive because `AM_STRATEGY_ENABLED = False`.

### P1 Anti-Martingale Settings

```python
AM_TRIGGER_CRASH = 8.0
AM_CASHOUT       = 7.0
AM_BET_AMOUNT    = 50
AM_MAX_STREAK    = 4
AM_MAX_BET       = 5000
```

If enabled, P1 would bet after a previous crash of at least `8.0x`, cash out at
`7.0x`, double after wins up to four consecutive wins, and reset after a loss.

### P2 Concurrent Anti-Martingale Settings

```python
P2_AM_ENABLED       = False
P2_AM_TRIGGER_CRASH = 8.0
P2_AM_CASHOUT       = 8.0
P2_AM_BET_AMOUNT    = 50
P2_AM_MAX_STREAK    = 4
P2_AM_MAX_BET       = 5000
```

P2 Anti-Martingale only runs if both `AM_STRATEGY_ENABLED = True` and
`P2_AM_ENABLED = True`.

## Summary Of Current Strategy

```text
Mode:          Smart two-panel recovery
P1 signal:     previous crash > 2.5x
P2 signal:     previous crash between 1.4x and 3.5x
P1 cashout:    2.5x
P2 cashout:    3.5x
Base stakes:   50 KES / 50 KES
Recovery:      enabled, capped at 5,000 KES target chunks in demo mode
Predictor:     enabled, break-even confidence gate
Stop profit:   +3,000 KES
Auto restart:  enabled
```

## Important Caveat

Aviator crash outcomes are gambling outcomes. The recovery system can reduce
small-session losses, but it also increases exposure after losing rounds. The
chunk cap, drawdown guard, stop-profit behavior, and demo-mode testing are
important parts of this strategy.
