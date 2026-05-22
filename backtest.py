#!/usr/bin/env python3
"""
Backtest prod_v2 strategy against all historical CSV + log data.

Usage:  python backtest.py
Output: full report printed to stdout + backtest_report.txt
"""

import csv
import os
import re
import statistics
from collections import defaultdict

# ── Directories ────────────────────────────────────────────────────────────────
HISTORY_DIR = "history"
LOG_DIR_ROOT = "."          # root-level logs (aviator_*.log)
LOG_DIR_LOGS = "logs"       # logs/ sub-directory

# ── Strategy parameters (exact mirror of current config.py / prod_v2) ──────────
BET_AMOUNT            = 50.0
P2_BET_AMOUNT         = 50.0
PANEL1_CASHOUT        = 2.5
PANEL2_CASHOUT        = 3.5
P1_TRIGGER_MULT       = 2.5
P2_LOW_STREAK_MIN     = 1.4
P2_LOW_STREAK_MAX     = 3.5
P2_LOW_STREAK_COUNT   = 1
P1_ASSIST_P2_ENABLED  = True
P1_ASSIST_PERCENTAGE  = 100
P1_ASSIST_TRIGGER_MAX = 1.4
P1_ASSIST_CASHOUT     = 1.4
RECOVERY_ENABLED      = True
RECOVERY_PROFIT_TARGET     = 25.0
RECOVERY_SCOPE        = "smart"
P2_RECOVERY_ENABLED   = True
P2_RECOVERY_PROFIT_TARGET  = 25.0
P2_RECOVERY_SCOPE     = "combined"
BURST_COOLDOWN        = 0
P2_ASSIST_P1_ENABLED  = False


def calc_p1_bet(p1_def, p2_def):
    if RECOVERY_SCOPE in ("combined", "smart"):
        target = p1_def + p2_def
    else:
        target = p1_def
    if target <= 0:
        return BET_AMOUNT
    net = max(0.01, PANEL1_CASHOUT - 1)
    return max(BET_AMOUNT, round((target + RECOVERY_PROFIT_TARGET) / net, 2))


def calc_p1_assist_bet(p2_def):
    if p2_def <= 0:
        return BET_AMOUNT
    target = p2_def * P1_ASSIST_PERCENTAGE / 100
    net = max(0.01, P1_ASSIST_CASHOUT - 1)
    return max(BET_AMOUNT, round((target + RECOVERY_PROFIT_TARGET) / net, 2))


# ── Data loading ───────────────────────────────────────────────────────────────

def load_csv_crashes():
    """Load all crash multipliers from history/*.csv in date order."""
    crashes = []
    files = sorted(
        f for f in os.listdir(HISTORY_DIR) if f.endswith(".csv")
    )
    for fname in files:
        path = os.path.join(HISTORY_DIR, fname)
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                try:
                    crashes.append((fname, float(row["crash_mult"])))
                except (ValueError, KeyError):
                    pass
    return crashes


def load_log_sessions():
    """
    Parse ROUND lines from all log files.
    Returns list of dicts: {file, round_no, crash, round_pnl, total_pnl, p1_bet, p2_bet}
    """
    ROUND_RE = re.compile(
        r"ROUND\s+(\d+)\s+\|.*crash=([\d.]+)x\s+\|\s+round=([+-]?[\d.]+)\s+KES\s+\|\s+total=([+-]?[\d.]+)\s+KES"
    )
    BET_RE = re.compile(r"P1=(?:WIN@[\d.]+x|LOSS)\(bet=([\d.]+)\).*P2=(?:WIN@[\d.]+x|LOSS)\(bet=([\d.]+)\)")

    log_files = []
    for fname in sorted(os.listdir(LOG_DIR_ROOT)):
        if fname.endswith(".log") and fname.startswith("aviator_"):
            log_files.append(os.path.join(LOG_DIR_ROOT, fname))
    if os.path.isdir(LOG_DIR_LOGS):
        for fname in sorted(os.listdir(LOG_DIR_LOGS)):
            if fname.endswith(".log"):
                log_files.append(os.path.join(LOG_DIR_LOGS, fname))

    sessions = []
    for lpath in log_files:
        rounds = []
        try:
            with open(lpath, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = ROUND_RE.search(line)
                    if m:
                        r = {
                            "file": os.path.basename(lpath),
                            "round_no": int(m.group(1)),
                            "crash": float(m.group(2)),
                            "round_pnl": float(m.group(3)),
                            "total_pnl": float(m.group(4)),
                        }
                        bm = BET_RE.search(line)
                        if bm:
                            r["p1_bet"] = float(bm.group(1))
                            r["p2_bet"] = float(bm.group(2))
                        else:
                            r["p1_bet"] = r["p2_bet"] = 0.0
                        rounds.append(r)
        except Exception:
            pass
        if rounds:
            sessions.append({"file": os.path.basename(lpath), "rounds": rounds})
    return sessions


# ── Strategy simulation ────────────────────────────────────────────────────────

def simulate(crash_list):
    """
    Full state-machine simulation of prod_v2 strategy.
    crash_list: list of floats (chronological)
    Returns dict of metrics + per-round log.
    """
    p1_bet_plan    = []
    p1_assist_plan = []
    p2_bet_plan    = []
    p1_cooldown    = 0
    p2_cooldown    = 0
    p1_def         = 0.0
    p2_def         = 0.0
    cumulative_pnl = 0.0
    highest_pnl    = 0.0
    lowest_pnl     = 0.0

    total_bets      = 0
    total_wins      = 0
    total_losses    = 0
    max_deficit     = 0.0
    max_single_bet  = 0.0
    consec_losses   = 0
    max_consec      = 0
    pnl_curve       = []
    bet_log         = []
    trigger_log     = []

    # track how long we are in recovery before clearing
    in_recovery_since = None
    recovery_durations = []

    for i, crash in enumerate(crash_list):
        history_slice = crash_list[max(0, i - 10): i + 1][::-1]

        p1_scheduled  = p1_bet_plan.pop(0)    if p1_bet_plan    else False
        p1_low_assist = p1_assist_plan.pop(0) if p1_assist_plan else False
        p2_scheduled  = p2_bet_plan.pop(0)    if p2_bet_plan    else False

        p1_assist = p1_low_assist and P1_ASSIST_P2_ENABLED and RECOVERY_ENABLED and p2_def > 0
        p2_assist = p1_scheduled and p1_def > 0 and P2_ASSIST_P1_ENABLED and P2_RECOVERY_ENABLED

        p1_this = p1_scheduled or p1_assist
        p2_this = p2_scheduled or p2_assist

        p1_recovery_leads = (
            p1_this and RECOVERY_ENABLED
            and RECOVERY_SCOPE in ("combined", "smart")
            and not p1_low_assist
            and (p1_def > 0 or p2_def > 0)
        )
        p2_suppressed    = p2_this and p1_recovery_leads
        p1_was_assisting = p1_this and p1_low_assist and P1_ASSIST_P2_ENABLED and p2_def > 0
        p1_cashout       = P1_ASSIST_CASHOUT if p1_was_assisting else PANEL1_CASHOUT

        # bet sizes
        if p1_this:
            p1_bet = calc_p1_assist_bet(p2_def) if p1_was_assisting else calc_p1_bet(p1_def, p2_def)
        else:
            p1_bet = 0.0
        p2_bet = P2_BET_AMOUNT if p2_this else 0.0

        if p1_this:
            max_single_bet = max(max_single_bet, p1_bet)
        if p2_this:
            max_single_bet = max(max_single_bet, p2_bet)

        if in_recovery_since is None and (p1_def > 0 or p2_def > 0):
            in_recovery_since = i

        round_pnl = 0.0
        if p1_this or p2_this:
            p1_win = crash >= p1_cashout if p1_this else False
            p2_win = crash >= PANEL2_CASHOUT if p2_this else False

            round_pnl += (p1_bet * (p1_cashout - 1)) if p1_win else (-p1_bet if p1_this else 0.0)
            round_pnl += (p2_bet * (PANEL2_CASHOUT - 1)) if p2_win else (-p2_bet if p2_this else 0.0)

            cumulative_pnl = round(cumulative_pnl + round_pnl, 2)
            highest_pnl    = max(highest_pnl, cumulative_pnl)
            lowest_pnl     = min(lowest_pnl,  cumulative_pnl)
            total_bets     += 1

            if round_pnl > 0:
                total_wins   += 1
                consec_losses = 0
            else:
                total_losses += 1
                consec_losses += 1
                max_consec    = max(max_consec, consec_losses)

            # ── update deficits ────────────────────────────────────────────────
            if p1_this:
                if p1_win:
                    if p1_was_assisting:
                        gain = round(p1_bet * (p1_cashout - 1), 2)
                        p2_def = max(0.0, round(p2_def - gain, 2))
                    else:
                        p1_def = 0.0
                        if RECOVERY_SCOPE in ("combined", "smart"):
                            p2_def = 0.0
                    p1_bet_plan = []; p1_assist_plan = []
                    p1_cooldown = BURST_COOLDOWN
                else:
                    if p1_was_assisting:
                        p1_def = round(p1_def + p1_bet, 2)
                    elif RECOVERY_ENABLED:
                        p1_def = round(p1_def + p1_bet, 2)
                    if not p1_bet_plan:
                        p1_cooldown = BURST_COOLDOWN

            if p2_this:
                if p2_win:
                    if not p2_suppressed:
                        if P2_RECOVERY_SCOPE == "combined":
                            p1_def = 0.0
                        p2_def = 0.0
                    p2_bet_plan = []
                    p2_cooldown = BURST_COOLDOWN
                else:
                    if not p2_suppressed and P2_RECOVERY_ENABLED:
                        p2_def = round(p2_def + p2_bet, 2)
                    if not p2_bet_plan:
                        p2_cooldown = BURST_COOLDOWN

            # priority clear
            if p1_recovery_leads and crash >= PANEL1_CASHOUT:
                p1_def = 0.0; p2_def = 0.0

            combined_def = round(p1_def + p2_def, 2)
            max_deficit  = max(max_deficit, combined_def)

            # recovery duration tracking
            if combined_def == 0 and in_recovery_since is not None:
                recovery_durations.append(i - in_recovery_since)
                in_recovery_since = None

            bet_log.append({
                "i":          i,
                "crash":      crash,
                "p1_this":    p1_this,
                "p2_this":    p2_this,
                "p1_bet":     p1_bet,
                "p2_bet":     p2_bet,
                "p1_win":     p1_win if p1_this else None,
                "p2_win":     p2_win if p2_this else None,
                "round_pnl":  round_pnl,
                "cum_pnl":    cumulative_pnl,
                "p1_def":     p1_def,
                "p2_def":     p2_def,
            })

        pnl_curve.append(cumulative_pnl)

        # ── check triggers ─────────────────────────────────────────────────────
        if not p1_bet_plan:
            if p1_cooldown > 0:
                p1_cooldown -= 1
            else:
                p1_trig_high   = crash > P1_TRIGGER_MULT
                p1_trig_assist = P1_ASSIST_P2_ENABLED and p2_def > 0 and crash <= P1_ASSIST_TRIGGER_MAX
                if p1_trig_assist:
                    p1_bet_plan    = [True]
                    p1_assist_plan = [True]
                    trigger_log.append((i, "P1_ASSIST", p2_def, crash))
                elif p1_trig_high:
                    p1_bet_plan    = [True]
                    p1_assist_plan = [False]
                    trigger_log.append((i, "P1_HIGH", p1_def + p2_def, crash))

        if not p2_bet_plan:
            if p2_cooldown > 0:
                p2_cooldown -= 1
            else:
                p2_trig_low = P2_LOW_STREAK_MIN < crash < P2_LOW_STREAK_MAX
                if p2_trig_low:
                    p2_bet_plan = [True]
                    trigger_log.append((i, "P2_LOW", p2_def, crash))

    return {
        "total_rounds":     len(crash_list),
        "total_bets":       total_bets,
        "total_wins":       total_wins,
        "total_losses":     total_losses,
        "final_pnl":        cumulative_pnl,
        "highest_pnl":      highest_pnl,
        "lowest_pnl":       lowest_pnl,
        "max_deficit":      max_deficit,
        "max_single_bet":   max_single_bet,
        "max_consec_losses":max_consec,
        "pnl_curve":        pnl_curve,
        "bet_log":          bet_log,
        "trigger_log":      trigger_log,
        "recovery_durations": recovery_durations,
        "crash_list":       crash_list,
    }


# ── Report builder ─────────────────────────────────────────────────────────────

def build_report(csv_crashes, sim, log_sessions):
    lines = []
    w = lines.append

    def sep(char="─", n=62):
        w(char * n)

    sep("═")
    w("  AVIATOR BOT — BACKTEST REPORT (prod_v2 strategy)")
    sep("═")

    # ── 1. Data summary ────────────────────────────────────────────────────────
    w("")
    w("DATA SOURCES")
    sep()
    w(f"  CSV files:       {len(set(s for s, _ in csv_crashes))} files")
    w(f"  Total CSV rows:  {len(csv_crashes):,} rounds")
    w(f"  Log files:       {len(log_sessions)} sessions with betting rounds")
    total_log_rounds = sum(len(s["rounds"]) for s in log_sessions)
    w(f"  Total log bets:  {total_log_rounds:,} bet rounds across all sessions")

    # ── 2. Crash distribution ──────────────────────────────────────────────────
    w("")
    w("CRASH MULTIPLIER DISTRIBUTION  (all {0:,} rounds)".format(len(csv_crashes)))
    sep()
    crashes = [c for _, c in csv_crashes]
    bands = [
        ("<1.5x",  lambda x: x < 1.5),
        ("1.5–2.5x", lambda x: 1.5 <= x < 2.5),
        ("2.5–3.5x", lambda x: 2.5 <= x < 3.5),
        ("3.5–5x", lambda x: 3.5 <= x < 5.0),
        ("5–10x",  lambda x: 5.0 <= x < 10.0),
        ("10–20x", lambda x: 10.0 <= x < 20.0),
        (">20x",   lambda x: x >= 20.0),
    ]
    for label, fn in bands:
        count = sum(1 for x in crashes if fn(x))
        pct   = count / len(crashes) * 100
        bar   = "█" * int(pct / 2)
        w(f"  {label:<12}  {count:>5,}  ({pct:5.1f}%)  {bar}")
    w(f"  Mean crash: {statistics.mean(crashes):.2f}x   Median: {statistics.median(crashes):.2f}x   Max: {max(crashes):.2f}x")

    # ── P1 trigger zone (crash > 2.5x) hit rate ────────────────────────────────
    p1_trig_count = sum(1 for x in crashes if x > P1_TRIGGER_MULT)
    p2_trig_count = sum(1 for x in crashes if P2_LOW_STREAK_MIN < x < P2_LOW_STREAK_MAX)
    w(f"  P1 trigger zone (>{P1_TRIGGER_MULT}x):         {p1_trig_count:>5,}  ({p1_trig_count/len(crashes)*100:.1f}%)")
    w(f"  P2 trigger zone ({P2_LOW_STREAK_MIN}–{P2_LOW_STREAK_MAX}x): {p2_trig_count:>5,}  ({p2_trig_count/len(crashes)*100:.1f}%)")

    # Win rates for each panel target
    p1_win_rate = sum(1 for x in crashes if x >= PANEL1_CASHOUT) / len(crashes) * 100
    p2_win_rate = sum(1 for x in crashes if x >= PANEL2_CASHOUT) / len(crashes) * 100
    p1a_win_rate = sum(1 for x in crashes if x >= P1_ASSIST_CASHOUT) / len(crashes) * 100
    w(f"  P1 cashout hit rate  (>={PANEL1_CASHOUT}x):  {p1_win_rate:.1f}%")
    w(f"  P2 cashout hit rate  (>={PANEL2_CASHOUT}x):  {p2_win_rate:.1f}%")
    w(f"  P1 assist hit rate   (>={P1_ASSIST_CASHOUT}x):  {p1a_win_rate:.1f}%")

    # Consecutive losses at P1 cashout
    consec = 0
    max_p1_consec = 0
    for x in crashes:
        if x < PANEL1_CASHOUT:
            consec += 1
            max_p1_consec = max(max_p1_consec, consec)
        else:
            consec = 0
    consec = 0
    max_p2_consec = 0
    for x in crashes:
        if x < PANEL2_CASHOUT:
            consec += 1
            max_p2_consec = max(max_p2_consec, consec)
        else:
            consec = 0
    w(f"  Max consecutive rounds below P1 cashout ({PANEL1_CASHOUT}x): {max_p1_consec}")
    w(f"  Max consecutive rounds below P2 cashout ({PANEL2_CASHOUT}x): {max_p2_consec}")

    # ── 3. Simulation results ──────────────────────────────────────────────────
    w("")
    w("SIMULATION RESULTS  (full replay on {0:,} rounds)".format(sim["total_rounds"]))
    sep()
    win_rate = sim["total_wins"] / sim["total_bets"] * 100 if sim["total_bets"] else 0
    w(f"  Rounds simulated:    {sim['total_rounds']:,}")
    w(f"  Betting rounds:      {sim['total_bets']:,}  ({sim['total_bets']/sim['total_rounds']*100:.1f}% of all rounds)")
    w(f"  Wins:                {sim['total_wins']:,}")
    w(f"  Losses:              {sim['total_losses']:,}")
    w(f"  Win rate:            {win_rate:.1f}%")
    w(f"  Final P&L:           KES {sim['final_pnl']:+,.2f}")
    w(f"  Best P&L reached:    KES {sim['highest_pnl']:+,.2f}")
    w(f"  Worst P&L reached:   KES {sim['lowest_pnl']:+,.2f}")
    w(f"  Max combined deficit:KES {sim['max_deficit']:,.2f}")
    w(f"  Max single bet size: KES {sim['max_single_bet']:,.2f}")
    w(f"  Max consec. losses:  {sim['max_consec_losses']}")

    # recovery duration
    rd = sim["recovery_durations"]
    if rd:
        w(f"  Recovery events:     {len(rd)}")
        w(f"  Avg rounds to clear: {statistics.mean(rd):.1f}  Max: {max(rd)}")

    # ── 4. Trigger breakdown ───────────────────────────────────────────────────
    w("")
    w("TRIGGER ANALYSIS")
    sep()
    trig_counts = defaultdict(int)
    trig_deficit_at = defaultdict(list)
    for _, ttype, deficit, crash in sim["trigger_log"]:
        trig_counts[ttype] += 1
        trig_deficit_at[ttype].append(deficit)
    for ttype, cnt in sorted(trig_counts.items()):
        defs = trig_deficit_at[ttype]
        avg_def = statistics.mean(defs) if defs else 0
        max_def = max(defs) if defs else 0
        w(f"  {ttype:<12}  {cnt:>4} triggers  avg deficit at trigger: KES {avg_def:>8.2f}  max: KES {max_def:>8.2f}")

    # ── 5. Dangerous scenarios ─────────────────────────────────────────────────
    w("")
    w("WORST DRAWDOWN EVENTS  (rounds where deficit > 500 KES)")
    sep()
    danger_rounds = [(r["i"], r["p1_def"] + r["p2_def"], r["p1_bet"], r["crash"])
                     for r in sim["bet_log"] if r["p1_def"] + r["p2_def"] > 500]
    if danger_rounds:
        for idx, (rnd, def_, bet, crash) in enumerate(danger_rounds[:15]):
            w(f"  Round {rnd:>5}: deficit KES {def_:>8.2f}  next P1 bet ~KES {calc_p1_bet(def_, 0):>8.2f}  crash was {crash:.2f}x")
        if len(danger_rounds) > 15:
            w(f"  ... and {len(danger_rounds)-15} more high-deficit rounds")
    else:
        w("  None — deficit never exceeded 500 KES in simulation")

    # ── 6. Real log session summary ───────────────────────────────────────────
    w("")
    w("ACTUAL SESSION RESULTS  (from log files)")
    sep()
    session_pnls = []
    all_real_bets = []
    for s in log_sessions:
        if not s["rounds"]:
            continue
        final = s["rounds"][-1]["total_pnl"]
        session_pnls.append(final)
        all_real_bets.extend(s["rounds"])

    if session_pnls:
        profitable = sum(1 for p in session_pnls if p > 0)
        w(f"  Sessions:            {len(session_pnls)}")
        w(f"  Profitable sessions: {profitable} / {len(session_pnls)}  ({profitable/len(session_pnls)*100:.0f}%)")
        w(f"  Best session P&L:    KES {max(session_pnls):+,.2f}")
        w(f"  Worst session P&L:   KES {min(session_pnls):+,.2f}")
        w(f"  Avg session P&L:     KES {statistics.mean(session_pnls):+,.2f}")
        w(f"  Total across all:    KES {sum(session_pnls):+,.2f}")
        if all_real_bets:
            real_bets_placed = [r for r in all_real_bets if r["p1_bet"] + r["p2_bet"] > 0]
            max_real_bet = max(r["p1_bet"] for r in all_real_bets) if all_real_bets else 0
            w(f"  Largest P1 bet seen: KES {max_real_bet:,.2f}")

    # ── 7. Risk analysis & recommendations ───────────────────────────────────
    w("")
    w("RISK ANALYSIS & IDENTIFIED WEAKNESSES")
    sep()

    # How often does deficit compound across sessions?
    large_deficits = [r for r in sim["bet_log"] if r["p1_def"] + r["p2_def"] > 300]
    w(f"  Rounds with deficit > KES 300:    {len(large_deficits)}")
    large_deficits_500 = [r for r in sim["bet_log"] if r["p1_def"] + r["p2_def"] > 500]
    w(f"  Rounds with deficit > KES 500:    {len(large_deficits_500)}")
    large_deficits_1000 = [r for r in sim["bet_log"] if r["p1_def"] + r["p2_def"] > 1000]
    w(f"  Rounds with deficit > KES 1000:   {len(large_deficits_1000)}")

    # How many consecutive P1 losses happen after trigger
    # (each P1 trigger = 1 round, so consecutive P1 TRIGGER losses)
    p1_trig_results = [(r["crash"] >= PANEL1_CASHOUT) for r in sim["bet_log"] if r["p1_this"]]
    consec_p1_loss = 0
    max_p1_trig_loss_streak = 0
    for won in p1_trig_results:
        if not won:
            consec_p1_loss += 1
            max_p1_trig_loss_streak = max(max_p1_trig_loss_streak, consec_p1_loss)
        else:
            consec_p1_loss = 0
    w(f"  Max consecutive P1 trigger losses: {max_p1_trig_loss_streak}")

    # estimate how much capital a worst-case P1 loss streak costs
    capital_at_risk = 0.0
    sample_def = 0.0
    for _ in range(max_p1_trig_loss_streak):
        bet = calc_p1_bet(sample_def, 0)
        capital_at_risk += bet
        sample_def += bet
    w(f"  Capital at risk in max P1 loss streak: KES {capital_at_risk:,.2f}  (deficit would reach KES {sample_def:,.2f})")
    w("")
    w("  KEY WEAKNESSES:")
    w(f"    1. Uncapped deficit — after {max_p1_trig_loss_streak} consecutive trigger losses P1 bets KES {calc_p1_bet(sample_def,0):,.2f}")
    w(f"    2. Assist bet on P1_ASSIST_CASHOUT={P1_ASSIST_CASHOUT}x is cheap to trigger but very large")
    w(f"       e.g. P2 deficit=500 → P1 assist bet = KES {calc_p1_assist_bet(500):,.2f}")
    w(f"    3. No cap on how large a single recovery bet can grow")
    w(f"    4. P1 trigger fires every round after a crash > 2.5x — could fire repeatedly")
    w(f"       during a high-crash streak, stacking unrecovered deficits")

    # ── 8. Proposed improvements ──────────────────────────────────────────────
    w("")
    w("PROPOSED RECOVERY IMPROVEMENTS  (safe, same strategy skeleton)")
    sep()
    w("""
  A) DEFICIT HARD CAP (RECOVERY_DEFICIT_CAP)
     When combined deficit >= cap, pause all new triggers and only let
     existing planned bets finish. This prevents runaway compounding.
     Recommended cap: KES 1,500 (3 × average recovery bet).

  B) MAX SINGLE BET LIMIT (MAX_RECOVERY_BET)
     Never let a single P1 or assist bet exceed this KES amount.
     Excess deficit above what can be recovered in one shot carries over.
     Recommended limit: KES 500.

  C) GRADUAL DEFICIT SPREAD (RECOVERY_SPREAD_ROUNDS)
     Instead of trying to recover all deficit in 1 round, spread across
     N rounds at a fraction each time. Makes individual bets smaller.
     e.g. spread=3: each round recovers 50% of remaining deficit.
     Already partially supported via RECOVERY_SCOPE="percentage" — just
     needs turning on with sensible step values.

  D) ASSIST BET CAP (MAX_ASSIST_BET)
     Cap the P1 assist bet separately (it bets at 1.4x cashout = risky).
     Recommended cap: KES 300 for assist mode.

  E) CONSECUTIVE TRIGGER LOSS GUARD (MAX_BURST_DEFICITS)
     Track how many consecutive trigger events failed. After N consecutive
     trigger losses without any win, take a watch-only cooldown of K rounds
     before accepting new triggers. Lets the market "reset".
     Recommended: N=3, K=5 cooldown rounds.
""")

    sep("═")
    w("  END OF REPORT")
    sep("═")

    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("Loading CSV history…")
    csv_crashes = load_csv_crashes()
    crashes_only = [c for _, c in csv_crashes]
    print(f"  {len(crashes_only):,} rounds loaded from CSVs")

    print("Loading log sessions…")
    log_sessions = load_log_sessions()
    print(f"  {len(log_sessions)} sessions, {sum(len(s['rounds']) for s in log_sessions):,} bet rounds")

    print("Running simulation…")
    sim = simulate(crashes_only)
    print("  Done.")

    report = build_report(csv_crashes, sim, log_sessions)
    print("\n" + report)

    out_path = "backtest_report.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\nReport saved to {out_path}")

    return sim


if __name__ == "__main__":
    main()
