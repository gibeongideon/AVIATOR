"""
Simulation: all historical CSVs combined into ONE continuous series.

Treats every round from every file as a single unbroken session starting
at 50,000 KES. Compares:
  ACTUAL       — actual round_pnl from CSV logs (different config at the time)
  NO PROTECT   — current config re-simulated, no drawdown protection
  WITH PROTECT — current config + drawdown protection (10% of INITIAL_BALANCE)

Key numbers:
  Chunk cap          = 5,000 KES  (10% of INITIAL_DEMO_BALANCE 50k)
  Drawdown threshold = 3,000 KES  (10% of INITIAL_BALANCE 30k)
  threshold < chunk cap → protection reduces bets when active

  Normal max P1 bet  : (5,000 + 50 + 25) / 1.5 = 3,383 KES
  Protected max P1 bet: (3,000 + 50 + 25) / 1.5 = 2,050 KES
"""

import csv
import os
import glob
from typing import List

# ── Config snapshot (must match current config.py) ─────────────────────────────
INITIAL_DEMO_BALANCE   = 50_000.0
INITIAL_BALANCE        = 30_000.0   # real bankroll — used for drawdown threshold
BET_AMOUNT             = 50.0
P2_BET_AMOUNT          = 50.0
PANEL1_CASHOUT         = 2.5
PANEL2_CASHOUT         = 3.5
RECOVERY_PROFIT_TARGET = 25.0
RECOVERY_ENABLED       = True
RECOVERY_SCOPE         = "smart"
P2_RECOVERY_ENABLED    = True
P2_RECOVERY_SCOPE      = "combined"
RECOVERY_CHUNK_CAP_PCT = 10
CHUNK_CAP              = round(INITIAL_DEMO_BALANCE * RECOVERY_CHUNK_CAP_PCT / 100, 2)  # 5,000
P1_TRIGGER_MULT        = 2.5
P1_TRIGGER_MULT_MAX    = float("inf")
P1_LOW_STREAK_MAX      = 0.0
P1_BET_PATTERN         = [1]
P2_LOW_STREAK_MIN      = 1.4
P2_LOW_STREAK_MAX      = 3.5
P2_BET_PATTERN         = [1]
MIN_TRIGGER_CRASH      = 1.22
DRAWDOWN_PROTECTION_PCT = 10.0
DRAWDOWN_THRESHOLD      = round(INITIAL_BALANCE * DRAWDOWN_PROTECTION_PCT / 100, 2)  # 3,000
DRAWDOWN_EXIT_FRAC      = 0.5


# ── Helpers ────────────────────────────────────────────────────────────────────

def _p1_bet_size(p1_def: float, p2_def: float, extra_risk: float = 0.0) -> float:
    target = p1_def + p2_def
    if target <= 0:
        return BET_AMOUNT
    if CHUNK_CAP > 0 and target > CHUNK_CAP:
        target = CHUNK_CAP
    net = max(0.01, PANEL1_CASHOUT - 1)
    return max(BET_AMOUNT, round((target + extra_risk + RECOVERY_PROFIT_TARGET) / net, 2))


def _p2_bet_size(p1_def: float, p2_def: float) -> float:
    target = p1_def + p2_def
    if target <= 0:
        return P2_BET_AMOUNT
    if CHUNK_CAP > 0 and target > CHUNK_CAP:
        target = CHUNK_CAP
    net = max(0.01, PANEL2_CASHOUT - 1)
    return max(P2_BET_AMOUNT, round((target + RECOVERY_PROFIT_TARGET) / net, 2))


def _cap_deficits(p1_def: float, p2_def: float, threshold: float):
    total = p1_def + p2_def
    if total <= threshold:
        return p1_def, p2_def
    ratio = p1_def / total if total > 0 else 1.0
    return round(threshold * ratio, 2), round(threshold * (1.0 - ratio), 2)


# ── Core simulator ──────────────────────────────────────────────────────────────

def simulate(crash_mults: List[float], drawdown_protection: bool = False) -> dict:
    cumulative_pnl  = 0.0
    peak_pnl        = 0.0
    p1_def          = 0.0
    p2_def          = 0.0
    p1_plan: List[int] = []
    p2_plan: List[int] = []
    dp_active       = False

    worst_bal    = INITIAL_DEMO_BALANCE
    max_drawdown = 0.0
    worst_round  = 0.0
    max_p1_bet   = 0.0
    max_p2_bet   = 0.0
    bet_rounds   = 0
    dp_on_rounds = 0
    running_peak = 0.0

    for crash in crash_mults:
        if cumulative_pnl > peak_pnl:
            peak_pnl = cumulative_pnl

        if drawdown_protection and DRAWDOWN_THRESHOLD > 0:
            drawdown = peak_pnl - cumulative_pnl
            if drawdown >= DRAWDOWN_THRESHOLD:
                if not dp_active:
                    dp_active = True
            elif dp_active and drawdown < DRAWDOWN_THRESHOLD * DRAWDOWN_EXIT_FRAC:
                dp_active = False

        if dp_active:
            dp_on_rounds += 1

        p1_this = bool(p1_plan.pop(0)) if p1_plan else False
        p2_this = bool(p2_plan.pop(0)) if p2_plan else False

        p1_recovery_leads = (
            p1_this and RECOVERY_ENABLED
            and RECOVERY_SCOPE in ("combined", "smart")
            and (p1_def > 0 or p2_def > 0)
        )
        p2_suppressed = p2_this and p1_recovery_leads

        if p1_this:
            _d1, _d2 = p1_def, p2_def
            if dp_active:
                _d1, _d2 = _cap_deficits(_d1, _d2, DRAWDOWN_THRESHOLD)
            p1_extra = P2_BET_AMOUNT if p2_suppressed else 0.0
            p1_bet = _p1_bet_size(_d1, _d2, extra_risk=p1_extra)
            if p1_bet > max_p1_bet:
                max_p1_bet = p1_bet
        else:
            p1_bet = 0.0

        if p2_this:
            if p2_suppressed:
                p2_bet = P2_BET_AMOUNT
            else:
                _d1, _d2 = p1_def, p2_def
                if dp_active:
                    _d1, _d2 = _cap_deficits(_d1, _d2, DRAWDOWN_THRESHOLD)
                p2_bet = _p2_bet_size(_d1, _d2)
            if p2_bet > max_p2_bet:
                max_p2_bet = p2_bet
        else:
            p2_bet = 0.0

        p1_win = p1_this and crash >= PANEL1_CASHOUT
        p2_win = p2_this and crash >= PANEL2_CASHOUT

        round_pnl = 0.0
        if p1_this:
            round_pnl += p1_bet * (PANEL1_CASHOUT - 1) if p1_win else -p1_bet
        if p2_this:
            round_pnl += p2_bet * (PANEL2_CASHOUT - 1) if p2_win else -p2_bet

        cumulative_pnl += round_pnl
        balance = INITIAL_DEMO_BALANCE + cumulative_pnl

        if p1_this or p2_this:
            bet_rounds += 1
        if round_pnl < worst_round:
            worst_round = round_pnl
        if balance < worst_bal:
            worst_bal = balance

        if cumulative_pnl > running_peak:
            running_peak = cumulative_pnl
        dd = running_peak - cumulative_pnl
        if dd > max_drawdown:
            max_drawdown = dd

        if p1_this:
            if p1_win:
                total = p1_def + p2_def
                chunk = min(total, CHUNK_CAP) if CHUNK_CAP > 0 else total
                leftover = max(0.0, round(total - chunk, 2))
                p1_def = leftover
                p2_def = 0.0
            else:
                p1_def = round(p1_def + p1_bet, 2)

        if p2_this and not (p1_this and p1_win):
            if p2_win:
                if not p2_suppressed:
                    p2_def = 0.0
            else:
                p2_def = round(p2_def + p2_bet, 2)

        gate = MIN_TRIGGER_CRASH > 0 and crash < MIN_TRIGGER_CRASH
        if not p1_plan and not gate:
            if P1_TRIGGER_MULT < crash <= P1_TRIGGER_MULT_MAX:
                p1_plan = list(P1_BET_PATTERN)
        if not p2_plan and not gate:
            if P2_LOW_STREAK_MIN < crash < P2_LOW_STREAK_MAX:
                p2_plan = list(P2_BET_PATTERN)

    return {
        "final_pnl":    round(cumulative_pnl, 2),
        "final_bal":    round(INITIAL_DEMO_BALANCE + cumulative_pnl, 2),
        "peak_pnl":     round(peak_pnl, 2),
        "peak_bal":     round(INITIAL_DEMO_BALANCE + peak_pnl, 2),
        "worst_bal":    round(worst_bal, 2),
        "max_drawdown": round(max_drawdown, 2),
        "worst_round":  round(worst_round, 2),
        "max_p1_bet":   round(max_p1_bet, 2),
        "max_p2_bet":   round(max_p2_bet, 2),
        "bet_rounds":   bet_rounds,
        "total_rounds": len(crash_mults),
        "dp_on_rounds": dp_on_rounds,
    }


# ── CSV loader ─────────────────────────────────────────────────────────────────

def load_csv(path: str):
    mults:   List[float] = []
    actuals: List[float] = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            mults.append(float(row["crash_mult"]))
            actuals.append(float(row["round_pnl"]))
    return mults, actuals


def actual_summary(actuals: List[float]) -> dict:
    cum = 0.0
    pk  = 0.0
    rpk = 0.0
    worst_bal  = INITIAL_DEMO_BALANCE
    max_dd     = 0.0
    worst_r    = 0.0
    for pnl in actuals:
        cum += pnl
        if cum > pk:
            pk = cum
        if cum > rpk:
            rpk = cum
        dd = rpk - cum
        if dd > max_dd:
            max_dd = dd
        b = INITIAL_DEMO_BALANCE + cum
        if b < worst_bal:
            worst_bal = b
        if pnl < worst_r:
            worst_r = pnl
    return {
        "final_pnl":    round(cum, 2),
        "final_bal":    round(INITIAL_DEMO_BALANCE + cum, 2),
        "peak_pnl":     round(pk, 2),
        "peak_bal":     round(INITIAL_DEMO_BALANCE + pk, 2),
        "worst_bal":    round(worst_bal, 2),
        "max_drawdown": round(max_dd, 2),
        "worst_round":  round(worst_r, 2),
        "bet_rounds":   sum(1 for p in actuals if p != 0.0),
        "total_rounds": len(actuals),
        "dp_on_rounds": 0,
    }


# ── Formatter ──────────────────────────────────────────────────────────────────

def fmt(v) -> str:
    if v is None:
        return "—"
    sign = "-" if v < 0 else "+"
    return f"{sign}{abs(v):,.0f}"


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    base = os.path.dirname(os.path.abspath(__file__))
    csv_files = sorted([
        f for f in glob.glob(os.path.join(base, "history", "aviator_2026*.csv"))
        if "New" not in f
    ])

    if not csv_files:
        print("No CSV files found in history/")
        return

    # Combine all files into one continuous sequence
    all_mults:   List[float] = []
    all_actuals: List[float] = []
    boundaries = []

    for fpath in csv_files:
        mults, actuals = load_csv(fpath)
        s = len(all_mults)
        all_mults.extend(mults)
        all_actuals.extend(actuals)
        boundaries.append((s + 1, len(all_mults), os.path.basename(fpath), len(mults)))

    W = 74
    print(f"\n{'═'*W}")
    print(f"  COMBINED SERIES — {len(csv_files)} files treated as one session")
    print(f"{'─'*W}")
    print(f"  {'File':<35} {'Rounds':>7}  {'Global rows':>12}")
    print(f"  {'-'*56}")
    for s, e, fname, n in boundaries:
        print(f"  {fname:<35} {n:>7,}  {s:>6,} – {e:<6,}")
    print(f"  {'TOTAL':<35} {len(all_mults):>7,}")
    print(f"{'─'*W}")
    print(f"  Start balance      : {INITIAL_DEMO_BALANCE:>10,.0f} KES")
    print(f"  Chunk cap          : {CHUNK_CAP:>10,.0f} KES  ({RECOVERY_CHUNK_CAP_PCT}% of demo balance)")
    print(f"  Drawdown threshold : {DRAWDOWN_THRESHOLD:>10,.0f} KES  ({DRAWDOWN_PROTECTION_PCT:.0f}% of INITIAL_BALANCE)")
    print(f"  Max P1 bet normal  : {(CHUNK_CAP+P2_BET_AMOUNT+RECOVERY_PROFIT_TARGET)/(PANEL1_CASHOUT-1):>10,.2f} KES")
    print(f"  Max P1 bet protect : {(DRAWDOWN_THRESHOLD+P2_BET_AMOUNT+RECOVERY_PROFIT_TARGET)/(PANEL1_CASHOUT-1):>10,.2f} KES")
    print(f"{'═'*W}")

    # Run sims
    act = actual_summary(all_actuals)
    nop = simulate(all_mults, drawdown_protection=False)
    wp  = simulate(all_mults, drawdown_protection=True)

    # Print comparison table
    print(f"\n  {'Metric':<30} {'ACTUAL':>12} {'NO PROTECT':>12} {'WITH PROTECT':>12}  NOTE")
    print(f"  {'-'*72}")

    def row(label, a, n, w, invert=False):
        """invert=True: lower value is better (drawdown, worst round, max bet)."""
        a_s = fmt(a) if a is not None else "—"
        n_s = fmt(n) if n is not None else "—"
        w_s = fmt(w) if w is not None else "—"
        note = ""
        if n is not None and w is not None:
            diff = w - n
            if abs(diff) >= 1:
                better = (diff < 0) if invert else (diff > 0)
                arrow = "✓" if better else "✗"
                note = f"  {arrow} {fmt(diff)}"
        print(f"  {label:<30} {a_s:>12} {n_s:>12} {w_s:>12}{note}")

    row("Final PnL (KES)",          act["final_pnl"],    nop["final_pnl"],    wp["final_pnl"])
    row("Final balance (KES)",       act["final_bal"],    nop["final_bal"],    wp["final_bal"])
    row("Peak PnL (KES)",            act["peak_pnl"],     nop["peak_pnl"],     wp["peak_pnl"])
    row("Peak balance (KES)",        act["peak_bal"],     nop["peak_bal"],     wp["peak_bal"])
    row("Worst balance (KES)",       act["worst_bal"],    nop["worst_bal"],    wp["worst_bal"],    invert=False)
    row("Max drawdown (KES)",        act["max_drawdown"], nop["max_drawdown"], wp["max_drawdown"], invert=True)
    row("Worst single round (KES)",  act["worst_round"],  nop["worst_round"],  wp["worst_round"],  invert=True)
    row("Max P1 bet (KES)",          None,                nop["max_p1_bet"],   wp["max_p1_bet"],   invert=True)
    row("Bet rounds",                act["bet_rounds"],   nop["bet_rounds"],   wp["bet_rounds"])
    row("Protection-ON rounds",      act["dp_on_rounds"], nop["dp_on_rounds"], wp["dp_on_rounds"])

    print(f"\n  {'─'*72}")
    print(f"  Balance walk summary:")
    print(f"")
    print(f"  {'':30} {'ACTUAL':>12} {'NO PROTECT':>12} {'WITH PROTECT':>12}")
    print(f"  {'Start':30} {INITIAL_DEMO_BALANCE:>12,.0f} {INITIAL_DEMO_BALANCE:>12,.0f} {INITIAL_DEMO_BALANCE:>12,.0f}")
    print(f"  {'Peak balance':30} {act['peak_bal']:>12,.0f} {nop['peak_bal']:>12,.0f} {wp['peak_bal']:>12,.0f}")
    print(f"  {'Worst balance':30} {act['worst_bal']:>12,.0f} {nop['worst_bal']:>12,.0f} {wp['worst_bal']:>12,.0f}")
    print(f"  {'Final balance':30} {act['final_bal']:>12,.0f} {nop['final_bal']:>12,.0f} {wp['final_bal']:>12,.0f}")
    print(f"  {'Max drawdown from peak':30} {act['max_drawdown']:>12,.0f} {nop['max_drawdown']:>12,.0f} {wp['max_drawdown']:>12,.0f}")
    print(f"{'═'*W}\n")


if __name__ == "__main__":
    main()
