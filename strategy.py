"""
Strategy deep-dive: progressive betting on the winning selective condition.

Finding from grid search: betting after prev crash >= 5x (or >= 8x) at
cashout levels 6x–8x produces a measurable positive edge on this dataset.

This script:
  1. Confirms the flat-bet edge at each (trigger, cashout) pair
  2. Tests 6 progressive systems on the best conditions
  3. Prints a final recommendation table
"""

import csv
import glob
import os
from typing import List, Tuple

# ── Constants ──────────────────────────────────────────────────────────────────
START_BAL   = 50_000.0
UNIT        = 50.0         # Base bet (KES)
MAX_BET     = 5_000.0      # Hard cap per bet for progressive systems
HISTORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history")


def load_all_crashes() -> List[float]:
    files = sorted([
        f for f in glob.glob(os.path.join(HISTORY_DIR, "aviator_2026*.csv"))
        if "New" not in f
    ])
    crashes: List[float] = []
    for fpath in files:
        with open(fpath, newline="") as f:
            for row in csv.DictReader(f):
                crashes.append(float(row["crash_mult"]))
    return crashes


# ── Selective bet sequence extractor ──────────────────────────────────────────

def get_bet_sequence(crashes: List[float], trigger: float, cashout: float) -> List[bool]:
    """
    Returns list of booleans: did the bet on round i win?
    A bet is placed on round i if crashes[i-1] >= trigger.
    Win = crashes[i] >= cashout.
    """
    results: List[bool] = []
    for i in range(1, len(crashes)):
        if crashes[i - 1] >= trigger:
            results.append(crashes[i] >= cashout)
    return results


# ── Flat-bet simulation ────────────────────────────────────────────────────────

def flat_sim(results: List[bool], cashout: float) -> dict:
    bal   = START_BAL
    pnl   = 0.0
    peak  = 0.0
    worst = START_BAL
    max_dd = 0.0
    rpk   = 0.0
    wins  = 0
    total_wagered = 0.0

    for win in results:
        total_wagered += UNIT
        if win:
            pnl += UNIT * (cashout - 1)
            wins += 1
        else:
            pnl -= UNIT
        bal = START_BAL + pnl
        if pnl > peak:
            peak = pnl
        if pnl > rpk:
            rpk = pnl
        dd = rpk - pnl
        if dd > max_dd:
            max_dd = dd
        if bal < worst:
            worst = bal

    win_rate  = wins / len(results) if results else 0.0
    break_even = 1.0 / cashout
    edge      = win_rate - break_even
    return {
        "final_pnl":   round(pnl, 2),
        "final_bal":   round(bal, 2),
        "peak_pnl":    round(peak, 2),
        "worst_bal":   round(worst, 2),
        "max_drawdown":round(max_dd, 2),
        "bets":        len(results),
        "wins":        wins,
        "win_rate":    round(win_rate * 100, 3),
        "break_even":  round(break_even * 100, 3),
        "edge_pct":    round(edge * 100, 3),
        "total_wagered": round(total_wagered, 2),
    }


# ── Progressive systems ────────────────────────────────────────────────────────

def _clamp(bet: float) -> float:
    return min(max(UNIT, round(bet, 2)), MAX_BET)


def martingale(results: List[bool], cashout: float, max_steps: int = 6) -> dict:
    bal = START_BAL
    pnl = 0.0
    peak = 0.0
    worst = START_BAL
    max_dd = 0.0
    rpk = 0.0
    step = 0
    wins = 0

    for win in results:
        bet = _clamp(UNIT * (2 ** step))
        if win:
            pnl += bet * (cashout - 1)
            wins += 1
            step = 0
        else:
            pnl -= bet
            step = min(step + 1, max_steps)
        bal = START_BAL + pnl
        if pnl > peak: peak = pnl
        if pnl > rpk: rpk = pnl
        dd = rpk - pnl
        if dd > max_dd: max_dd = dd
        if bal < worst: worst = bal

    return {"final_pnl": round(pnl, 2), "final_bal": round(bal, 2),
            "worst_bal": round(worst, 2), "max_drawdown": round(max_dd, 2),
            "bets": len(results), "wins": wins}


def fibonacci_sim(results: List[bool], cashout: float) -> dict:
    fibs = [1, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89]
    bal = START_BAL
    pnl = 0.0
    peak = 0.0
    worst = START_BAL
    max_dd = 0.0
    rpk = 0.0
    idx = 0
    wins = 0

    for win in results:
        bet = _clamp(UNIT * fibs[min(idx, len(fibs) - 1)])
        if win:
            pnl += bet * (cashout - 1)
            wins += 1
            idx = max(0, idx - 2)
        else:
            pnl -= bet
            idx = min(idx + 1, len(fibs) - 1)
        bal = START_BAL + pnl
        if pnl > peak: peak = pnl
        if pnl > rpk: rpk = pnl
        dd = rpk - pnl
        if dd > max_dd: max_dd = dd
        if bal < worst: worst = bal

    return {"final_pnl": round(pnl, 2), "final_bal": round(bal, 2),
            "worst_bal": round(worst, 2), "max_drawdown": round(max_dd, 2),
            "bets": len(results), "wins": wins}


def one_three_two_six(results: List[bool], cashout: float) -> dict:
    sequence = [1, 3, 2, 6]
    bal = START_BAL
    pnl = 0.0
    peak = 0.0
    worst = START_BAL
    max_dd = 0.0
    rpk = 0.0
    pos = 0
    wins = 0

    for win in results:
        bet = _clamp(UNIT * sequence[pos])
        if win:
            pnl += bet * (cashout - 1)
            wins += 1
            pos = (pos + 1) % len(sequence)
        else:
            pnl -= bet
            pos = 0
        bal = START_BAL + pnl
        if pnl > peak: peak = pnl
        if pnl > rpk: rpk = pnl
        dd = rpk - pnl
        if dd > max_dd: max_dd = dd
        if bal < worst: worst = bal

    return {"final_pnl": round(pnl, 2), "final_bal": round(bal, 2),
            "worst_bal": round(worst, 2), "max_drawdown": round(max_dd, 2),
            "bets": len(results), "wins": wins}


def paroli(results: List[bool], cashout: float, levels: int = 3) -> dict:
    bal = START_BAL
    pnl = 0.0
    peak = 0.0
    worst = START_BAL
    max_dd = 0.0
    rpk = 0.0
    streak = 0
    wins = 0

    for win in results:
        bet = _clamp(UNIT * (2 ** streak))
        if win:
            pnl += bet * (cashout - 1)
            wins += 1
            streak = (streak + 1) % levels
        else:
            pnl -= bet
            streak = 0
        bal = START_BAL + pnl
        if pnl > peak: peak = pnl
        if pnl > rpk: rpk = pnl
        dd = rpk - pnl
        if dd > max_dd: max_dd = dd
        if bal < worst: worst = bal

    return {"final_pnl": round(pnl, 2), "final_bal": round(bal, 2),
            "worst_bal": round(worst, 2), "max_drawdown": round(max_dd, 2),
            "bets": len(results), "wins": wins}


def oscars_grind(results: List[bool], cashout: float) -> dict:
    """
    Oscar's Grind: aim to win exactly 1 UNIT per 'session'.
    Session target = current_bet after a win would produce +1 unit profit.
    On win: increase bet by 1 unit (up to session completion).
    On loss: keep same bet.
    """
    bal = START_BAL
    pnl = 0.0
    peak = 0.0
    worst = START_BAL
    max_dd = 0.0
    rpk = 0.0
    wins = 0

    session_pnl = 0.0   # PnL for current grind session
    bet = UNIT

    for win in results:
        actual_bet = _clamp(bet)
        win_amount = actual_bet * (cashout - 1)
        if win:
            pnl += win_amount
            session_pnl += win_amount
            wins += 1
            if session_pnl >= UNIT:
                # Session complete — reset
                session_pnl = 0.0
                bet = UNIT
            else:
                # Increase by 1 unit but don't overshoot target
                # Max bet that won't exceed the target
                deficit = UNIT - session_pnl
                needed_bet = deficit / (cashout - 1)
                new_bet = actual_bet + UNIT
                bet = min(new_bet, needed_bet + actual_bet, MAX_BET)
                bet = max(UNIT, round(bet, 2))
        else:
            pnl -= actual_bet
            session_pnl -= actual_bet
            # Keep bet the same on a loss

        bal = START_BAL + pnl
        if pnl > peak: peak = pnl
        if pnl > rpk: rpk = pnl
        dd = rpk - pnl
        if dd > max_dd: max_dd = dd
        if bal < worst: worst = bal

    return {"final_pnl": round(pnl, 2), "final_bal": round(bal, 2),
            "worst_bal": round(worst, 2), "max_drawdown": round(max_dd, 2),
            "bets": len(results), "wins": wins}


def anti_martingale(results: List[bool], cashout: float, max_streak: int = 4) -> dict:
    """Double bet after each win (up to max_streak), reset on loss."""
    bal = START_BAL
    pnl = 0.0
    peak = 0.0
    worst = START_BAL
    max_dd = 0.0
    rpk = 0.0
    streak = 0
    wins = 0

    for win in results:
        bet = _clamp(UNIT * (2 ** streak))
        if win:
            pnl += bet * (cashout - 1)
            wins += 1
            if streak < max_streak - 1:
                streak += 1
            else:
                streak = 0  # Take profit and reset
        else:
            pnl -= bet
            streak = 0
        bal = START_BAL + pnl
        if pnl > peak: peak = pnl
        if pnl > rpk: rpk = pnl
        dd = rpk - pnl
        if dd > max_dd: max_dd = dd
        if bal < worst: worst = bal

    return {"final_pnl": round(pnl, 2), "final_bal": round(bal, 2),
            "worst_bal": round(worst, 2), "max_drawdown": round(max_dd, 2),
            "bets": len(results), "wins": wins}


# ── Report helpers ─────────────────────────────────────────────────────────────

def fk(v: float) -> str:
    sign = "-" if v < 0 else "+"
    return f"{sign}{abs(v):,.0f}"


def pct(v: float) -> str:
    return f"{v:+.2f}%"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    crashes = load_all_crashes()
    total   = len(crashes)
    W       = 80

    print(f"\n{'═'*W}")
    print(f"  STRATEGY ANALYSIS — {total:,} rounds across all historical CSVs")
    print(f"  Base bet: {UNIT:.0f} KES  |  Max bet cap: {MAX_BET:,.0f} KES  |  Start: {START_BAL:,.0f} KES")
    print(f"{'═'*W}")

    # ── 1. Flat-bet edge table across (trigger, cashout) pairs ─────────────────
    triggers  = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0]
    cashouts  = [2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0, 15.0, 20.0]

    print(f"\n  FLAT BET EDGE SCAN  (trigger = previous crash ≥ X, bet next round)")
    print(f"  {'Trigger':>8}  {'Cashout':>8}  {'Bets':>6}  {'Wins':>6}  "
          f"{'Win%':>7}  {'BE%':>7}  {'Edge%':>8}  {'PnL':>10}")
    print(f"  {'-'*72}")

    best_rows = []
    for trig in triggers:
        for cash in cashouts:
            seq = get_bet_sequence(crashes, trig, cash)
            if len(seq) < 30:
                continue
            r = flat_sim(seq, cash)
            if r["edge_pct"] > 0:
                best_rows.append((trig, cash, r))
                marker = " ◀" if r["edge_pct"] > 1.0 else ""
                print(f"  {trig:>8.1f}  {cash:>8.1f}  {len(seq):>6,}  {r['wins']:>6,}  "
                      f"{r['win_rate']:>6.2f}%  {r['break_even']:>6.2f}%  "
                      f"{r['edge_pct']:>+7.3f}%  {fk(r['final_pnl']):>10}{marker}")

    if not best_rows:
        print("  (no positive edge found in this dataset)")

    # ── 2. Best conditions — progressive system comparison ────────────────────
    top_conditions = sorted(best_rows, key=lambda x: x[2]["edge_pct"], reverse=True)[:5]

    if top_conditions:
        print(f"\n{'─'*W}")
        print(f"  PROGRESSIVE SYSTEMS on top {len(top_conditions)} conditions")
        print(f"  (Flat-bet edge > 1%)")
        print(f"{'─'*W}")

        systems = [
            ("Flat",             lambda seq, co: flat_sim(seq, co)),
            ("Martingale×2 c6",  lambda seq, co: martingale(seq, co, max_steps=6)),
            ("Fibonacci",        lambda seq, co: fibonacci_sim(seq, co)),
            ("1-3-2-6",          lambda seq, co: one_three_two_six(seq, co)),
            ("Paroli ×3",        lambda seq, co: paroli(seq, co, levels=3)),
            ("Oscar's Grind",    lambda seq, co: oscars_grind(seq, co)),
            ("Anti-Mart ×4",     lambda seq, co: anti_martingale(seq, co, max_streak=4)),
        ]

        for trig, cash, flat_r in top_conditions:
            seq = get_bet_sequence(crashes, trig, cash)
            print(f"\n  Trigger: prev ≥ {trig:.0f}x → bet at {cash:.0f}x cashout "
                  f"({len(seq):,} bets, {flat_r['win_rate']:.2f}% win rate, "
                  f"{flat_r['edge_pct']:+.3f}% edge)")
            print(f"  {'System':<20} {'PnL':>10} {'Final bal':>12} "
                  f"{'Worst bal':>12} {'Max DD':>10} {'Wins':>6}")
            print(f"  {'-'*70}")
            for name, fn in systems:
                try:
                    r = fn(seq, cash)
                    print(f"  {name:<20} {fk(r['final_pnl']):>10} "
                          f"{r['final_bal']:>12,.0f} "
                          f"{r['worst_bal']:>12,.0f} "
                          f"{fk(-r['max_drawdown']):>10} "
                          f"{r['wins']:>6,}")
                except Exception as e:
                    print(f"  {name:<20} ERROR: {e}")

    # ── 3. Streak analysis ─────────────────────────────────────────────────────
    print(f"\n{'─'*W}")
    print(f"  LOSING STREAK ANALYSIS — best conditions")
    print(f"{'─'*W}")

    for trig, cash, _ in top_conditions[:3]:
        seq = get_bet_sequence(crashes, trig, cash)
        max_loss = 0
        cur_loss = 0
        streak_counts = {}
        for win in seq:
            if not win:
                cur_loss += 1
            else:
                if cur_loss > 0:
                    streak_counts[cur_loss] = streak_counts.get(cur_loss, 0) + 1
                    max_loss = max(max_loss, cur_loss)
                cur_loss = 0
        if cur_loss > 0:
            streak_counts[cur_loss] = streak_counts.get(cur_loss, 0) + 1
            max_loss = max(max_loss, cur_loss)

        total_seqs = sum(streak_counts.values())
        print(f"\n  prev≥{trig:.0f}x @ {cash:.0f}x — max losing streak: {max_loss}")
        print(f"  {'Streak len':>12} {'Count':>8} {'% of all sequences':>20}")
        for k in sorted(streak_counts.keys())[:15]:
            pct_val = streak_counts[k] / total_seqs * 100
            print(f"  {k:>12} {streak_counts[k]:>8,} {pct_val:>19.1f}%")

    # ── 4. Summary recommendation ─────────────────────────────────────────────
    print(f"\n{'═'*W}")
    print(f"  RECOMMENDATION SUMMARY")
    print(f"{'═'*W}")
    if best_rows:
        best_flat = sorted(best_rows, key=lambda x: x[2]["final_pnl"], reverse=True)[0]
        trig, cash, r = best_flat
        seq  = get_bet_sequence(crashes, trig, cash)
        og_r = oscars_grind(seq, cash)
        mg_r = martingale(seq, cash, max_steps=5)

        print(f"\n  Best flat-bet condition: prev crash ≥ {trig:.0f}x → bet at {cash:.0f}x")
        print(f"  Rounds played  : {r['bets']:,} / {total:,} ({r['bets']/total*100:.1f}% of all rounds)")
        print(f"  Win rate       : {r['win_rate']:.3f}%  (break-even {r['break_even']:.3f}%)")
        print(f"  Edge           : {r['edge_pct']:+.3f}% per bet")
        print(f"  Flat PnL       : {fk(r['final_pnl'])} KES  (worst bal {r['worst_bal']:,.0f})")
        print(f"  Oscar's Grind  : {fk(og_r['final_pnl'])} KES  (worst bal {og_r['worst_bal']:,.0f})")
        print(f"  Martingale ×5  : {fk(mg_r['final_pnl'])} KES  (worst bal {mg_r['worst_bal']:,.0f})")
        print()
    else:
        print("\n  No condition with positive edge found.")
    print(f"{'═'*W}\n")


if __name__ == "__main__":
    main()
