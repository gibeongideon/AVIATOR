#!/usr/bin/env python3
"""
Aviator strategy backtest — runs all CSVs in history/ and reports per-session
and aggregate results using the exact same logic as bot.py.
"""

import csv
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import config  # noqa: E402  (local import after path fix)


# ── Helpers ───────────────────────────────────────────────────────────────────

def effective_chunk_cap() -> float:
    pct = getattr(config, "RECOVERY_CHUNK_CAP_PCT", 0)
    if pct > 0:
        bal = (getattr(config, "INITIAL_DEMO_BALANCE", 0)
               if config.DEMO_MODE else getattr(config, "INITIAL_BALANCE", 0))
        if bal > 0:
            return round(bal * pct / 100, 2)
    return getattr(config, "RECOVERY_CHUNK_CAP", 0)


def calc_p1_bet(p1d: float, p2d: float = 0.0, step: int = 0) -> float:
    if not config.RECOVERY_ENABLED:
        return config.BET_AMOUNT
    if config.RECOVERY_SCOPE == "individual":
        target = p1d if p1d > 0 else 0.0
    elif config.RECOVERY_SCOPE in ("combined", "smart"):
        target = p1d + p2d
    else:
        total = p1d + p2d
        max_s = config.RECOVERY_STEPS if config.RECOVERY_STEPS > 0 else config.P1_MAX_BET_ROUNDS
        target = total if (step + 1) >= max_s else total * config.RECOVERY_PERCENTAGE / 100
    if target <= 0:
        return config.BET_AMOUNT
    cap = effective_chunk_cap()
    if cap > 0 and target > cap:
        target = cap
    return max(config.BET_AMOUNT,
               round((target + config.RECOVERY_PROFIT_TARGET) / max(0.01, config.PANEL1_CASHOUT - 1), 2))


def calc_p2_bet(p1d: float, p2d: float, step: int = 0) -> float:
    if not config.P2_RECOVERY_ENABLED:
        return config.P2_BET_AMOUNT
    if p1d > 0 and config.P2_ASSIST_P1_ENABLED:
        t = p1d * config.P2_ASSIST_PERCENTAGE / 100
        return max(config.P2_BET_AMOUNT,
                   round((t + config.P2_RECOVERY_PROFIT_TARGET) / max(0.01, config.PANEL2_CASHOUT - 1), 2))
    if config.P2_RECOVERY_SCOPE in ("individual", "smart"):
        target = p2d
    elif config.P2_RECOVERY_SCOPE == "combined":
        target = p1d + p2d
    else:
        total = p1d + p2d
        max_s = config.P2_RECOVERY_STEPS if config.P2_RECOVERY_STEPS > 0 else config.P2_MAX_BET_ROUNDS
        target = total if (step + 1) >= max_s else total * config.P2_RECOVERY_PERCENTAGE / 100
    if target <= 0:
        return config.P2_BET_AMOUNT
    cap = effective_chunk_cap()
    if cap > 0 and target > cap:
        target = cap
    return max(config.P2_BET_AMOUNT,
               round((target + config.P2_RECOVERY_PROFIT_TARGET) / max(0.01, config.PANEL2_CASHOUT - 1), 2))


def calc_p1_assist_p2_bet(p2d: float) -> float:
    if not config.P1_ASSIST_P2_ENABLED or p2d <= 0:
        return config.BET_AMOUNT
    target = p2d * config.P1_ASSIST_PERCENTAGE / 100
    cap = effective_chunk_cap()
    if cap > 0 and target > cap:
        target = cap
    return max(config.BET_AMOUNT,
               round((target + config.RECOVERY_PROFIT_TARGET) / max(0.01, config.P1_ASSIST_CASHOUT - 1), 2))


def check_stop(cumulative_pnl: float, peak_pnl: float):
    """Returns (stop_reason, updated_peak_pnl) or (None, updated_peak_pnl)."""
    if cumulative_pnl > peak_pnl:
        peak_pnl = cumulative_pnl

    stop_on_loss   = getattr(config, "STOP_ON_LOSS", 0)
    stop_on_profit = getattr(config, "STOP_ON_PROFIT", 0)
    stop_on_dd_pct = getattr(config, "STOP_ON_DRAWDOWN_PCT", 0)

    if stop_on_loss < 0 and cumulative_pnl <= stop_on_loss:
        return f"Loss limit (KES {cumulative_pnl:.2f})", peak_pnl

    if stop_on_dd_pct > 0 and stop_on_profit > 0:
        if peak_pnl >= stop_on_profit:
            allowed = peak_pnl * stop_on_dd_pct / 100
            drawdown = peak_pnl - cumulative_pnl
            if drawdown >= allowed:
                return (f"Drawdown {drawdown:.2f} KES from peak {peak_pnl:.2f} KES "
                        f"(now {cumulative_pnl:.2f} KES)"), peak_pnl
    elif stop_on_profit > 0 and cumulative_pnl >= stop_on_profit:
        return f"Profit target (KES {cumulative_pnl:.2f})", peak_pnl

    return None, peak_pnl


# ── Session simulation ─────────────────────────────────────────────────────────

def run_session(crashes: list[float]) -> dict:
    cumulative_pnl   = 0.0
    peak_pnl         = 0.0
    p1_deficit       = 0.0
    p2_deficit       = 0.0
    p1_bet           = config.BET_AMOUNT
    p2_bet           = config.P2_BET_AMOUNT
    p1_step          = 0
    p2_step          = 0
    p1_cooldown      = 0
    p2_cooldown      = 0

    p1_bet_plan      = []
    p1_assist_plan   = []
    p1_follow_plan   = []
    p1_low_zone_plan = []
    p2_bet_plan      = []

    p1_pattern = list(config.P1_BET_PATTERN)
    p2_pattern = list(config.P2_BET_PATTERN)

    min_trigger  = getattr(config, "MIN_TRIGGER_CRASH", 0.0)
    lz_enabled   = getattr(config, "P1_LOW_ZONE_ENABLED", False)
    lz_max       = getattr(config, "P1_LOW_ZONE_MAX", 1.4)
    lz_cashout   = getattr(config, "P1_LOW_ZONE_CASHOUT", 1.5)
    lz_pct       = getattr(config, "P1_LOW_ZONE_PERCENTAGE", 50)
    p2_low_min   = getattr(config, "P2_LOW_STREAK_MIN", 0.0)
    p1_mult_max  = getattr(config, "P1_TRIGGER_MULT_MAX", float("inf"))

    rounds       = 0
    stop_reason  = None
    max_p1_bet   = 0.0
    max_p2_bet   = 0.0
    history      = []  # history[0] = most recent crash

    for crash in crashes:

        # ── 1. Check stop before this round ──────────────────────────────────
        stop_reason, peak_pnl = check_stop(cumulative_pnl, peak_pnl)
        if stop_reason:
            break

        # ── 2. Pop from plans ─────────────────────────────────────────────────
        p1_scheduled  = p1_bet_plan.pop(0)      if p1_bet_plan      else False
        p1_low_assist = p1_assist_plan.pop(0)   if p1_assist_plan   else False
        p1_follow     = p1_follow_plan.pop(0)   if p1_follow_plan   else False
        p1_low_zone   = p1_low_zone_plan.pop(0) if p1_low_zone_plan else False
        p2_scheduled  = p2_bet_plan.pop(0)      if p2_bet_plan      else False

        p2_assist_this = (p1_scheduled
                          and p1_deficit > 0
                          and config.P2_ASSIST_P1_ENABLED
                          and config.P2_RECOVERY_ENABLED)
        p1_assist_this = (p1_low_assist
                          and config.P1_ASSIST_P2_ENABLED
                          and config.RECOVERY_ENABLED
                          and p2_deficit > 0)
        p1_this = p1_scheduled or p1_assist_this or p1_follow or p1_low_zone
        p2_this = p2_scheduled or p2_assist_this

        p1_recovery_leads = (p1_this
                             and config.RECOVERY_ENABLED
                             and config.RECOVERY_SCOPE in ("combined", "smart")
                             and not p1_low_assist
                             and (p1_deficit > 0 or p2_deficit > 0))
        p2_suppressed  = p2_this and p1_recovery_leads
        p1_assisting   = p1_this and p1_low_assist and config.P1_ASSIST_P2_ENABLED and config.RECOVERY_ENABLED and p2_deficit > 0
        p2_assisting   = p2_assist_this
        p1_cashout     = (config.P1_ASSIST_CASHOUT if p1_assisting
                          else lz_cashout          if p1_low_zone
                          else config.PANEL1_CASHOUT)

        # ── 3. Compute bets ───────────────────────────────────────────────────
        if p1_this:
            if p1_assisting:
                p1_bet = calc_p1_assist_p2_bet(p2_deficit)
            elif p1_low_zone:
                lz_t = p1_deficit * lz_pct / 100
                lz_n = max(0.01, lz_cashout - 1)
                p1_bet = (max(config.BET_AMOUNT, round((lz_t + config.RECOVERY_PROFIT_TARGET) / lz_n, 2))
                          if lz_t > 0 else config.BET_AMOUNT)
            elif p1_follow:
                p1_bet = config.BET_AMOUNT
            else:
                p1_bet = calc_p1_bet(p1_deficit, p2_deficit, p1_step)

        if p2_this:
            p2_bet = (config.P2_BET_AMOUNT if p2_suppressed
                      else calc_p2_bet(p1_deficit, p2_deficit, p2_step))

        # ── 4. Process results ────────────────────────────────────────────────
        if p1_this or p2_this:
            rounds += 1
            p1_used = p1_bet if p1_this else 0.0
            p2_used = p2_bet if p2_this else 0.0
            max_p1_bet = max(max_p1_bet, p1_used)
            max_p2_bet = max(max_p2_bet, p2_used)

            p1_win = crash >= p1_cashout
            p2_win = crash >= config.PANEL2_CASHOUT

            rnd_pnl = 0.0
            if p1_this:
                rnd_pnl += p1_used * (p1_cashout - 1) if p1_win else -p1_used
            if p2_this:
                rnd_pnl += p2_used * (config.PANEL2_CASHOUT - 1) if p2_win else -p2_used
            cumulative_pnl = round(cumulative_pnl + rnd_pnl, 2)

            # ── P1 result ──────────────────────────────────────────────────────
            if p1_this:
                if p1_win:
                    if p1_follow:
                        pass  # no deficit change; win is just a bonus
                    elif p1_low_zone:
                        gain = round(p1_used * (lz_cashout - 1), 2)
                        p1_deficit = max(0.0, round(p1_deficit - gain, 2))
                    elif p1_assisting:
                        gain = round(p1_used * (p1_cashout - 1), 2)
                        p2_deficit = max(0.0, round(p2_deficit - gain, 2))
                        if p2_deficit <= 0:
                            p2_step = 0
                    elif config.RECOVERY_SCOPE == "percentage":
                        total = p1_deficit + p2_deficit
                        max_s = config.RECOVERY_STEPS if config.RECOVERY_STEPS > 0 else config.P1_MAX_BET_ROUNDS
                        tgt   = total if (p1_step + 1) >= max_s else total * config.RECOVERY_PERCENTAGE / 100
                        new   = round(max(0.0, total - tgt), 2)
                        p1_deficit = new
                        p2_deficit = 0.0
                    else:
                        covers_p2 = config.RECOVERY_SCOPE in ("combined", "smart")
                        total = p1_deficit + (p2_deficit if covers_p2 else 0.0)
                        cap   = effective_chunk_cap()
                        chunk = min(total, cap) if cap > 0 else total
                        left  = max(0.0, round(total - chunk, 2))
                        p1_deficit = left
                        if covers_p2:
                            p2_deficit = 0.0

                    p1_bet_plan = []
                    p1_assist_plan = []
                    p1_follow_plan = []
                    p1_low_zone_plan = []
                    p1_cooldown = config.BURST_COOLDOWN
                else:
                    if p1_follow:
                        p1_deficit = round(p1_deficit + p1_used, 2)
                    elif p1_low_zone:
                        p1_deficit = round(p1_deficit + p1_used, 2)
                    elif p1_assisting:
                        p1_deficit = round(p1_deficit + p1_used, 2)
                    elif config.RECOVERY_ENABLED:
                        p1_deficit = round(p1_deficit + p1_used, 2)
                    if not p1_bet_plan:
                        p1_cooldown = config.BURST_COOLDOWN

                if config.RECOVERY_SCOPE == "percentage" and config.RECOVERY_ENABLED:
                    total = p1_deficit + p2_deficit
                    if total <= 0:
                        p1_step = 0
                    else:
                        max_s  = config.RECOVERY_STEPS if config.RECOVERY_STEPS > 0 else config.P1_MAX_BET_ROUNDS
                        p1_step = 0 if (p1_step + 1) >= max_s else p1_step + 1

            # ── P2 result ──────────────────────────────────────────────────────
            if p2_this:
                if p2_win:
                    if p2_suppressed:
                        pass  # P1 recovery leads; P2 win is a bonus only
                    elif p2_assisting:
                        gain = round(p2_used * (config.PANEL2_CASHOUT - 1), 2)
                        p1_deficit = max(0.0, round(p1_deficit - gain, 2))
                    elif config.P2_RECOVERY_SCOPE == "percentage":
                        max_s = config.P2_RECOVERY_STEPS if config.P2_RECOVERY_STEPS > 0 else config.P2_MAX_BET_ROUNDS
                        was_last = (p2_step + 1) >= max_s
                        tgt = p2_deficit if was_last else p2_deficit * config.P2_RECOVERY_PERCENTAGE / 100
                        p2_deficit = round(max(0.0, p2_deficit - tgt), 2)
                    else:
                        if config.P2_RECOVERY_SCOPE == "combined":
                            total = p1_deficit + p2_deficit
                            cap   = effective_chunk_cap()
                            chunk = min(total, cap) if cap > 0 else total
                            left  = max(0.0, round(total - chunk, 2))
                            p1_deficit = left
                        p2_deficit = 0.0

                    p2_bet_plan = []
                    p2_cooldown = config.BURST_COOLDOWN
                else:
                    if p2_suppressed:
                        pass
                    elif p2_assisting:
                        p2_deficit = round(p2_deficit + p2_used, 2)
                    elif config.P2_RECOVERY_ENABLED:
                        p2_deficit = round(p2_deficit + p2_used, 2)
                    if not p2_bet_plan:
                        p2_cooldown = config.BURST_COOLDOWN

                if config.P2_RECOVERY_SCOPE == "percentage" and config.P2_RECOVERY_ENABLED:
                    total = p1_deficit + p2_deficit
                    if total <= 0:
                        p2_step = 0
                    else:
                        max_s   = config.P2_RECOVERY_STEPS if config.P2_RECOVERY_STEPS > 0 else config.P2_MAX_BET_ROUNDS
                        p2_step = 0 if (p2_step + 1) >= max_s else p2_step + 1

            # ── P1 priority recovery: when P1 leads and wins, clear all ───────
            if p1_recovery_leads and p1_win:
                p1_deficit = 0.0
                p2_deficit = 0.0
                p1_step    = 0
                p2_step    = 0

        # ── 5. Update history (most recent first) ──────────────────────────────
        history = [crash] + history

        # ── 6. Trigger check for NEXT round ───────────────────────────────────
        gate = min_trigger > 0 and crash < min_trigger

        if not p1_bet_plan and not gate:
            if p1_cooldown > 0:
                p1_cooldown -= 1
            else:
                p1_trig_high = config.P1_TRIGGER_MULT < crash <= p1_mult_max
                p1_trig_assist = (config.P1_ASSIST_P2_ENABLED
                                  and p2_deficit > 0
                                  and crash <= config.P1_ASSIST_TRIGGER_MAX)
                recent = history[:config.P1_LOW_STREAK_COUNT]
                p1_trig_low = (len(recent) >= config.P1_LOW_STREAK_COUNT
                               and all(m <= config.P1_LOW_STREAK_MAX for m in recent))
                p1_trig_lz  = (lz_enabled and p1_deficit > 0 and crash <= lz_max)

                if p1_trig_assist or p1_trig_high or p1_trig_low or p1_trig_lz:
                    p1_bet_plan      = list(p1_pattern)
                    p1_assist_plan   = [p1_trig_assist and bool(s) for s in p1_bet_plan]
                    p1_low_zone_plan = [p1_trig_lz and bool(s) for s in p1_bet_plan]

        if not p2_bet_plan and not gate:
            if p2_cooldown > 0:
                p2_cooldown -= 1
            else:
                recent = history[:config.P2_LOW_STREAK_COUNT]
                p2_trig_low = (len(recent) >= config.P2_LOW_STREAK_COUNT
                               and all(p2_low_min < m < config.P2_LOW_STREAK_MAX for m in recent))
                if p2_trig_low:
                    p2_bet_plan = list(p2_pattern)

        # ── Follow logic ───────────────────────────────────────────────────────
        if not gate:
            if p1_bet_plan and not p2_bet_plan and getattr(config, "P2_FOLLOW_P1", False):
                p2_bet_plan = list(p2_pattern)
            if p2_bet_plan and not p1_bet_plan and getattr(config, "P1_FOLLOW_P2", False):
                p1_bet_plan    = list(p1_pattern)
                p1_follow_plan = [True] * len(p1_bet_plan)

    return {
        "rounds":         rounds,
        "final_pnl":      cumulative_pnl,
        "peak_pnl":       peak_pnl,
        "stop_reason":    stop_reason or "All rounds completed",
        "max_p1_bet":     max_p1_bet,
        "max_p2_bet":     max_p2_bet,
        "p1_deficit_end": p1_deficit,
        "p2_deficit_end": p2_deficit,
    }


# ── CSV loader ─────────────────────────────────────────────────────────────────

def load_crashes(filepath: str) -> list[float]:
    crashes = []
    with open(filepath, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                crashes.append(float(row["crash_mult"]))
            except (KeyError, ValueError):
                pass
    return crashes


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    history_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history")
    csvfiles = sorted(f for f in os.listdir(history_dir) if f.endswith(".csv"))

    cap = effective_chunk_cap()
    dd_pct = getattr(config, "STOP_ON_DRAWDOWN_PCT", 0)
    stop_profit = getattr(config, "STOP_ON_PROFIT", 0)

    print()
    print("=" * 72)
    print("  AVIATOR BACKTEST")
    print(f"  P1: {config.PANEL1_CASHOUT}x cashout | P2: {config.PANEL2_CASHOUT}x cashout")
    print(f"  Base bets — P1: {config.BET_AMOUNT} KES | P2: {config.P2_BET_AMOUNT} KES")
    print(f"  Chunk cap: {cap:.0f} KES  ({getattr(config,'RECOVERY_CHUNK_CAP_PCT',0)}% of "
          f"{'demo' if config.DEMO_MODE else 'real'} balance "
          f"{getattr(config,'INITIAL_DEMO_BALANCE',0) if config.DEMO_MODE else getattr(config,'INITIAL_BALANCE',0):,} KES)")
    print(f"  STOP_ON_PROFIT: {stop_profit} KES  |  drawdown: {dd_pct}% of peak  |  "
          f"STOP_ON_LOSS: {getattr(config,'STOP_ON_LOSS',0)}")
    print(f"  P1 scope: {config.RECOVERY_SCOPE}  |  P2 scope: {config.P2_RECOVERY_SCOPE}")
    print(f"  MIN_TRIGGER_CRASH: {getattr(config,'MIN_TRIGGER_CRASH',0)}x  |  "
          f"P1_ASSIST: {'on' if config.P1_ASSIST_P2_ENABLED else 'off'}  |  "
          f"P1_LOW_ZONE: {'on' if getattr(config,'P1_LOW_ZONE_ENABLED',False) else 'off'}")
    print("=" * 72)
    print()

    total_pnl = 0.0
    results   = []

    for fname in csvfiles:
        path = os.path.join(history_dir, fname)
        crashes = load_crashes(path)
        if not crashes:
            print(f"  {fname}: no crash data — skipped")
            continue

        r = run_session(crashes)
        total_pnl += r["final_pnl"]
        results.append((fname, r))

        ok   = r["final_pnl"] >= 0
        icon = "+" if ok else "-"
        print(f"  [{icon}] {fname}  ({len(crashes)} crashes → {r['rounds']} rounds bet)")
        print(f"        PnL: {r['final_pnl']:+.2f} KES   Peak: +{r['peak_pnl']:.2f} KES")
        print(f"        Max bet — P1: {r['max_p1_bet']:.2f} KES   P2: {r['max_p2_bet']:.2f} KES")
        print(f"        Stop: {r['stop_reason']}")
        if r["p1_deficit_end"] > 0 or r["p2_deficit_end"] > 0:
            print(f"        Remaining deficit — P1: {r['p1_deficit_end']:.2f}  P2: {r['p2_deficit_end']:.2f} KES")
        print()

    profitable = sum(1 for _, r in results if r["final_pnl"] >= 0)
    print("=" * 72)
    print(f"  Sessions: {len(results)}   Profitable: {profitable}/{len(results)}")
    print(f"  TOTAL PnL: {total_pnl:+.2f} KES")
    print("=" * 72)
    print()


if __name__ == "__main__":
    main()
