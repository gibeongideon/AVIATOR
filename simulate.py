"""
Simulation: replay all historical CSVs through the current bot strategy.
Compares ACTUAL (from CSV) vs WITHOUT_PROTECTION (fresh sim) vs WITH_PROTECTION (new feature).

Usage:
    python simulate.py
"""

import csv
import os
import glob
from dataclasses import dataclass, field
from typing import List, Optional

# ── Config snapshot (must match running config.py) ─────────────────────────────
INITIAL_BALANCE          = 50_000.0   # demo balance used in these sessions
BET_AMOUNT               = 50.0
P2_BET_AMOUNT            = 50.0
PANEL1_CASHOUT           = 2.5
PANEL2_CASHOUT           = 3.5
RECOVERY_PROFIT_TARGET   = 25.0
RECOVERY_ENABLED         = True
RECOVERY_SCOPE           = "smart"       # P1 covers both deficits
P2_RECOVERY_ENABLED      = True
P2_RECOVERY_SCOPE        = "combined"
RECOVERY_CHUNK_CAP_PCT   = 10            # % of INITIAL_BALANCE_FOR_CAP
INITIAL_BALANCE_FOR_CAP  = 30_000.0     # config.INITIAL_BALANCE (separate from demo balance)
P1_TRIGGER_MULT          = 2.5
P1_TRIGGER_MULT_MAX      = float("inf")
P1_LOW_STREAK_MAX        = 0.0           # 0 = disabled
P1_LOW_STREAK_COUNT      = 1
P1_BET_PATTERN           = [1]
P2_TRIGGER_MULT          = 3.5
P2_TRIGGER_MULT_MAX      = 0.0           # 0 = disabled (P2 uses low-zone only)
P2_LOW_STREAK_MIN        = 1.4
P2_LOW_STREAK_MAX        = 3.5
P2_LOW_STREAK_COUNT      = 1
P2_BET_PATTERN           = [1]
MIN_TRIGGER_CRASH        = 1.22
DRAWDOWN_PROTECTION_PCT  = 10.0          # 10% of INITIAL_BALANCE = 5 000 KES
DRAWDOWN_EXIT_FRAC       = 0.5           # exit when drawdown < threshold * this

CHUNK_CAP = INITIAL_BALANCE_FOR_CAP * RECOVERY_CHUNK_CAP_PCT / 100   # 3 000 KES
DRAWDOWN_THRESHOLD = INITIAL_BALANCE * DRAWDOWN_PROTECTION_PCT / 100  # 5 000 KES


# ── Helpers ────────────────────────────────────────────────────────────────────

def _p1_bet_size(p1_def: float, p2_def: float, extra_risk: float = 0.0) -> float:
    if not RECOVERY_ENABLED:
        return BET_AMOUNT
    target = p1_def + p2_def   # smart scope
    if target <= 0:
        return BET_AMOUNT
    if CHUNK_CAP > 0 and target > CHUNK_CAP:
        target = CHUNK_CAP
    net = max(0.01, PANEL1_CASHOUT - 1)
    return max(BET_AMOUNT, round((target + extra_risk + RECOVERY_PROFIT_TARGET) / net, 2))


def _p2_bet_size(p1_def: float, p2_def: float) -> float:
    if not P2_RECOVERY_ENABLED:
        return P2_BET_AMOUNT
    target = p1_def + p2_def   # combined scope
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


@dataclass
class RoundResult:
    crash: float
    p1_bet: float
    p2_bet: float
    round_pnl: float
    cumulative_pnl: float
    balance: float
    peak_pnl: float
    dp_active: bool
    p1_def: float
    p2_def: float


def simulate(crash_mults: List[float], drawdown_protection: bool = False) -> List[RoundResult]:
    cumulative_pnl    = 0.0
    peak_pnl          = 0.0
    p1_def            = 0.0
    p2_def            = 0.0
    p1_bet_plan: List[int] = []
    p2_bet_plan: List[int] = []
    dp_active         = False
    results           = []

    for crash in crash_mults:
        # ── Update peak and drawdown protection ──────────────────────────────
        if cumulative_pnl > peak_pnl:
            peak_pnl = cumulative_pnl

        if drawdown_protection and DRAWDOWN_THRESHOLD > 0:
            drawdown = peak_pnl - cumulative_pnl
            if drawdown >= DRAWDOWN_THRESHOLD:
                dp_active = True
            elif dp_active and drawdown < DRAWDOWN_THRESHOLD * DRAWDOWN_EXIT_FRAC:
                dp_active = False

        # ── Determine bets for this round ─────────────────────────────────────
        p1_this = bool(p1_bet_plan.pop(0)) if p1_bet_plan else False
        p2_this = bool(p2_bet_plan.pop(0)) if p2_bet_plan else False

        # P1 leads recovery (smart scope) when it's betting with any deficit
        p1_recovery_leads = (
            p1_this
            and RECOVERY_ENABLED
            and RECOVERY_SCOPE in ("combined", "smart")
            and (p1_def > 0 or p2_def > 0)
        )
        p2_suppressed = p2_this and p1_recovery_leads

        # ── Calculate bet sizes ───────────────────────────────────────────────
        if p1_this:
            _pd1, _pd2 = (p1_def, p2_def)
            if dp_active:
                _pd1, _pd2 = _cap_deficits(_pd1, _pd2, DRAWDOWN_THRESHOLD)
            p1_extra = P2_BET_AMOUNT if p2_suppressed else 0.0
            p1_bet = _p1_bet_size(_pd1, _pd2, extra_risk=p1_extra)
        else:
            p1_bet = 0.0

        if p2_this:
            if p2_suppressed:
                p2_bet = P2_BET_AMOUNT
            else:
                _pd1, _pd2 = (p1_def, p2_def)
                if dp_active:
                    _pd1, _pd2 = _cap_deficits(_pd1, _pd2, DRAWDOWN_THRESHOLD)
                p2_bet = _p2_bet_size(_pd1, _pd2)
        else:
            p2_bet = 0.0

        # ── Resolve round ────────────────────────────────────────────────────
        p1_win = p1_this and crash >= PANEL1_CASHOUT
        p2_win = p2_this and crash >= PANEL2_CASHOUT

        round_pnl = 0.0
        if p1_this:
            round_pnl += p1_bet * (PANEL1_CASHOUT - 1) if p1_win else -p1_bet
        if p2_this:
            round_pnl += p2_bet * (PANEL2_CASHOUT - 1) if p2_win else -p2_bet

        cumulative_pnl += round_pnl

        # ── Update deficits ──────────────────────────────────────────────────
        if p1_this:
            if p1_win:
                # smart scope: P1 win clears both deficits (subject to chunk cap)
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

        # ── Triggers for NEXT round ──────────────────────────────────────────
        gate_blocked = MIN_TRIGGER_CRASH > 0 and crash < MIN_TRIGGER_CRASH

        if not p1_bet_plan and not gate_blocked:
            p1_trig_high = P1_TRIGGER_MULT < crash <= P1_TRIGGER_MULT_MAX
            p1_trig_low  = (P1_LOW_STREAK_MAX > 0 and crash <= P1_LOW_STREAK_MAX)
            if p1_trig_high or p1_trig_low:
                p1_bet_plan = list(P1_BET_PATTERN)

        if not p2_bet_plan and not gate_blocked:
            p2_trig_low = P2_LOW_STREAK_MIN < crash < P2_LOW_STREAK_MAX
            if p2_trig_low:
                p2_bet_plan = list(P2_BET_PATTERN)

        results.append(RoundResult(
            crash          = crash,
            p1_bet         = p1_bet,
            p2_bet         = p2_bet,
            round_pnl      = round_pnl,
            cumulative_pnl = cumulative_pnl,
            balance        = INITIAL_BALANCE + cumulative_pnl,
            peak_pnl       = peak_pnl,
            dp_active      = dp_active,
            p1_def         = p1_def,
            p2_def         = p2_def,
        ))

    return results


def load_csv(path: str):
    """Return (crash_mults, actual_pnl_by_round, initial_balance_from_csv)."""
    mults   = []
    actuals = []
    start_bal = None
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            mults.append(float(row["crash_mult"]))
            actuals.append(float(row["round_pnl"]))
            if start_bal is None:
                if "running_balance_after_bet" in row:
                    bal_str = row["running_balance_after_bet"].replace(",", "").replace(" KES", "").strip()
                    start_bal = float(bal_str) - float(row["bankroll_change"])
                else:
                    start_bal = INITIAL_BALANCE
    return mults, actuals, start_bal or INITIAL_BALANCE


def summary(results: List[RoundResult]):
    if not results:
        return {}
    final_pnl   = results[-1].cumulative_pnl
    peak_pnl    = max(r.peak_pnl for r in results)
    worst_bal   = min(r.balance for r in results)
    bet_rounds  = sum(1 for r in results if r.p1_bet > 0 or r.p2_bet > 0)
    max_p1_bet  = max((r.p1_bet for r in results), default=0)
    max_p2_bet  = max((r.p2_bet for r in results), default=0)
    dp_rounds   = sum(1 for r in results if r.dp_active)
    total_rounds = len(results)

    # Worst single-round loss
    worst_round = min((r.round_pnl for r in results), default=0)

    # Max drawdown from peak (in KES)
    max_drawdown = 0.0
    running_peak = 0.0
    for r in results:
        if r.cumulative_pnl > running_peak:
            running_peak = r.cumulative_pnl
        dd = running_peak - r.cumulative_pnl
        if dd > max_drawdown:
            max_drawdown = dd

    return {
        "final_pnl":    round(final_pnl, 2),
        "peak_pnl":     round(peak_pnl, 2),
        "worst_bal":    round(worst_bal, 2),
        "max_drawdown": round(max_drawdown, 2),
        "worst_round":  round(worst_round, 2),
        "max_p1_bet":   round(max_p1_bet, 2),
        "max_p2_bet":   round(max_p2_bet, 2),
        "bet_rounds":   bet_rounds,
        "total_rounds": total_rounds,
        "dp_rounds":    dp_rounds,
    }


def actual_summary(crash_mults, actuals, start_bal):
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    worst_bal = start_bal
    worst_round = 0.0
    for pnl in actuals:
        cumulative += pnl
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_drawdown:
            max_drawdown = dd
        bal = start_bal + cumulative
        if bal < worst_bal:
            worst_bal = bal
        if pnl < worst_round:
            worst_round = pnl
    bet_rounds = sum(1 for p in actuals if p != 0.0)
    return {
        "final_pnl":    round(cumulative, 2),
        "peak_pnl":     round(peak, 2),
        "worst_bal":    round(worst_bal, 2),
        "max_drawdown": round(max_drawdown, 2),
        "worst_round":  round(worst_round, 2),
        "bet_rounds":   bet_rounds,
        "total_rounds": len(actuals),
        "dp_rounds":    0,
    }


def kes(v: float) -> str:
    sign = "-" if v < 0 else "+"
    return f"{sign}{abs(v):,.0f}"


def print_report(fname: str, act: dict, no_p: dict, with_p: dict, start_bal: float):
    print(f"\n{'═'*72}")
    print(f"  {os.path.basename(fname)}")
    print(f"  Start balance: {start_bal:,.0f} KES   |   Rounds: {act['total_rounds']}")
    print(f"{'─'*72}")
    header = f"  {'Metric':<26} {'ACTUAL':>12} {'NO PROTECT':>12} {'WITH PROTECT':>13}"
    print(header)
    print(f"  {'-'*68}")

    rows = [
        ("Final PnL (KES)",     act["final_pnl"],    no_p["final_pnl"],    with_p["final_pnl"]),
        ("Peak PnL (KES)",      act["peak_pnl"],      no_p["peak_pnl"],     with_p["peak_pnl"]),
        ("Worst balance (KES)", act["worst_bal"],     no_p["worst_bal"],    with_p["worst_bal"]),
        ("Max drawdown (KES)",  act["max_drawdown"],  no_p["max_drawdown"], with_p["max_drawdown"]),
        ("Worst round (KES)",   act["worst_round"],   no_p["worst_round"],  with_p["worst_round"]),
        ("Max P1 bet (KES)",    None,                 no_p["max_p1_bet"],   with_p["max_p1_bet"]),
        ("Bet rounds",          act["bet_rounds"],    no_p["bet_rounds"],   with_p["bet_rounds"]),
        ("Protection-ON rounds",act["dp_rounds"],     no_p["dp_rounds"],    with_p["dp_rounds"]),
    ]
    for label, a_val, n_val, w_val in rows:
        a_str = kes(a_val) if a_val is not None else "  —"
        n_str = kes(n_val) if n_val is not None else "  —"
        w_str = kes(w_val) if w_val is not None else "  —"
        # Highlight improvement in max_drawdown / worst_round / worst_bal
        marker = ""
        if label in ("Max drawdown (KES)", "Max P1 bet (KES)") and w_val is not None and n_val is not None:
            saved = n_val - w_val
            if saved > 0:
                marker = f"  ← saved {kes(saved)}"
        if label == "Worst balance (KES)" and w_val is not None and n_val is not None:
            saved = w_val - n_val
            if saved > 0:
                marker = f"  ← +{saved:,.0f} KES floor"
        if label == "Final PnL (KES)" and w_val is not None and n_val is not None:
            diff = w_val - n_val
            if abs(diff) > 0:
                marker = f"  ← {kes(diff)} vs no-protect"
        print(f"  {label:<26} {a_str:>12} {n_str:>12} {w_str:>13}{marker}")

    print(f"{'─'*72}")


def main():
    history_dir = os.path.join(os.path.dirname(__file__), "history")
    csv_files = sorted(glob.glob(os.path.join(history_dir, "aviator_2026*.csv")))
    # Exclude the New/ subdirectory files
    csv_files = [f for f in csv_files if "New" not in f]

    if not csv_files:
        print("No CSV files found.")
        return

    print(f"\n{'═'*72}")
    print(f"  AVIATOR BOT — DRAWDOWN PROTECTION SIMULATION")
    print(f"  Config: INITIAL_BALANCE={INITIAL_BALANCE:,.0f} KES")
    print(f"          P1_CASHOUT={PANEL1_CASHOUT}x  P2_CASHOUT={PANEL2_CASHOUT}x")
    print(f"          P1_TRIGGER>{P1_TRIGGER_MULT}x  P2_ZONE={P2_LOW_STREAK_MIN}x–{P2_LOW_STREAK_MAX}x")
    print(f"          CHUNK_CAP={CHUNK_CAP:,.0f} KES  DRAWDOWN_THRESHOLD={DRAWDOWN_THRESHOLD:,.0f} KES (10%)")
    print(f"{'═'*72}")

    totals_act   = {"final_pnl": 0, "max_drawdown": 0, "worst_bal": 0}
    totals_nop   = {"final_pnl": 0, "max_drawdown": 0, "worst_bal": 0}
    totals_wp    = {"final_pnl": 0, "max_drawdown": 0, "worst_bal": 0}

    for fpath in csv_files:
        crash_mults, actuals, start_bal = load_csv(fpath)
        act   = actual_summary(crash_mults, actuals, start_bal)
        no_p  = summary(simulate(crash_mults, drawdown_protection=False))
        with_p = summary(simulate(crash_mults, drawdown_protection=True))
        print_report(fpath, act, no_p, with_p, start_bal)

        totals_act["final_pnl"]   += act["final_pnl"]
        totals_act["max_drawdown"] = max(totals_act["max_drawdown"], act["max_drawdown"])
        totals_nop["final_pnl"]   += no_p["final_pnl"]
        totals_nop["max_drawdown"] = max(totals_nop["max_drawdown"], no_p["max_drawdown"])
        totals_wp["final_pnl"]    += with_p["final_pnl"]
        totals_wp["max_drawdown"]  = max(totals_wp["max_drawdown"], with_p["max_drawdown"])

    print(f"\n{'═'*72}")
    print(f"  TOTALS ACROSS ALL SESSIONS")
    print(f"{'─'*72}")
    print(f"  {'Metric':<26} {'ACTUAL':>12} {'NO PROTECT':>12} {'WITH PROTECT':>13}")
    print(f"  {'-'*68}")
    print(f"  {'Cumulative PnL (KES)':<26} {kes(totals_act['final_pnl']):>12} "
          f"{kes(totals_nop['final_pnl']):>12} {kes(totals_wp['final_pnl']):>13}")
    print(f"  {'Single-session peak DD':<26} {kes(totals_act['max_drawdown']):>12} "
          f"{kes(totals_nop['max_drawdown']):>12} {kes(totals_wp['max_drawdown']):>13}")
    print(f"{'═'*72}\n")


if __name__ == "__main__":
    main()
