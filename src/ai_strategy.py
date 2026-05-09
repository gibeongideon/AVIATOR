"""
AI Strategy — crash pattern analyzer.

Called every round when strategy_type == "ai", after the bot has collected
AI_HISTORY_WINDOW crashes.  Returns a dict with any subset of:

    bet_amount      — P1 base bet (KES)
    p2_bet_amount   — P2 base bet (KES)
    panel1_cashout  — P1 auto-cashout multiplier
    panel2_cashout  — P2 auto-cashout multiplier

Missing keys → the strategy's baseline defaults are used unchanged.
Empty dict → no change this round (manual overrides via POST /sessions/{id}/ai-params still apply).
"""


def analyze(history: list[float]) -> dict:
    """
    Analyze the last N crash multipliers and return parameter suggestions.

    Args:
        history: The most recent N crash values (newest first), where N == AI_HISTORY_WINDOW.
                 Already sliced by the caller — len(history) == window.

    Returns:
        Dict with zero or more of: bet_amount, p2_bet_amount, panel1_cashout, panel2_cashout.

    ── Extension point ──────────────────────────────────────────────────────────
    Replace the `return {}` below with real pattern logic.  Example skeleton:

        recent     = history          # already window-sized, newest first
        mean       = sum(recent) / len(recent)
        high_count = sum(1 for x in recent if x > 5)
        low_streak = sum(1 for x in recent if x <= 2)

        if mean < 2.0:
            # cold market — lower cashout targets for easier wins
            return {"panel1_cashout": 3.0, "panel2_cashout": 2.0, "bet_amount": 1.0}

        if high_count >= 4:
            # plenty of big crashes lately — push targets higher
            return {"panel1_cashout": 10.0, "panel2_cashout": 5.0}

        if low_streak >= 6:
            # long cold streak — bet more conservatively
            return {"panel1_cashout": 2.5, "panel2_cashout": 2.0, "bet_amount": 0.5}

        return {}
    ─────────────────────────────────────────────────────────────────────────────
    """
    if len(history) == 0:
        return {}

    # ── Add pattern analysis here ─────────────────────────────────────────────
    # (Phase 1: no automated adjustments — manual overrides only)
    # ─────────────────────────────────────────────────────────────────────────

    return {}
