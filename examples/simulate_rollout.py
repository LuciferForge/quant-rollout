#!/usr/bin/env python3
"""Live demo: walk a rollout through the full state machine.

Shows:
  1. Stage 0 → 1 advance after gate met
  2. Veto window timing
  3. Kill switch trip on a losing streak
  4. Auto-revert to stage 0
  5. Operator clear + re-advance

Run:
    python examples/simulate_rollout.py

Output is human-readable. Each line is one tick.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from quant_rollout import (
    Stage, Gate, KillSwitch, Rollout,
    RolloutAction, TradeOutcome,
)
from quant_rollout.core import RolloutState


def fmt_decision(d, label=""):
    return (
        f"  [{label:>10}] action={d.action.value:14s} "
        f"stage={d.from_stage}→{d.to_stage}  "
        f"n={d.metrics.n:3d} WR={d.metrics.win_rate*100:5.1f}% "
        f"ev/$={d.metrics.ev_per_dollar*100:+5.1f}¢  "
        f"reason={d.reason}"
    )


def main():
    tmpdir = Path(tempfile.mkdtemp(prefix="qr-demo-"))
    state_path = tmpdir / "rollout_state.json"
    print(f"State file: {state_path}")
    print()

    clock = [1_000_000.0]

    stages = [
        Stage(num=0, params={"max_entry": 0.30}, name="baseline"),
        Stage(
            num=1,
            params={"max_entry": 0.50},
            gate=Gate(min_n=10, min_win_rate=0.60, min_ev_per_dollar=0.04),
            name="canary",
        ),
        Stage(
            num=2,
            params={"max_entry": 0.70, "position_size": 10.0},
            gate=Gate(min_n=20, min_win_rate=0.65, min_ev_per_dollar=0.05),
            name="full",
        ),
    ]
    ks = KillSwitch(
        wr_lookback=10, wr_threshold=0.50,
        ev_lookback=15, ev_threshold=-0.10,
    )
    r = Rollout(
        stages=stages,
        kill_switch=ks,
        state_path=state_path,
        veto_window_seconds=300,
        clock_now_unix=lambda: clock[0],
        clock_now_iso=lambda: "2026-05-01T12:00:00+00:00",
    )

    def winner(i, base_ts="2026-05-01T00"):
        return TradeOutcome(pnl=2.0, size=5.0, timestamp=f"{base_ts}:{i:02d}:00+00:00")

    def loser(i, base_ts="2026-06-01T00"):
        return TradeOutcome(pnl=-3.0, size=5.0, timestamp=f"{base_ts}:{i:02d}:00+00:00")

    print("=" * 80)
    print("PHASE 1 — Bot is at stage 0 (baseline). Need 10 trades + 60% WR + 4¢/$ to advance.")
    print("=" * 80)
    print(fmt_decision(r.tick([]), "empty"))
    print(fmt_decision(r.tick([winner(i) for i in range(5)]), "5 wins"))
    print(fmt_decision(r.tick([winner(i) for i in range(10)]), "10 wins"))

    print()
    print("=" * 80)
    print("PHASE 2 — Gate just met. Veto window opens (300s). Operator can /veto in window.")
    print("=" * 80)
    state = RolloutState.load(state_path)
    print(f"  state: pending_to={state.advance_pending_to}, "
          f"deadline_unix={state.advance_veto_deadline_unix}")
    clock[0] = 1_000_100.0  # 100s in
    print(fmt_decision(r.tick([winner(i) for i in range(10)]), "+100s"))
    clock[0] = 1_000_400.0  # 400s in (past deadline)
    print(fmt_decision(r.tick([winner(i) for i in range(10)]), "+400s (expired)"))

    state = RolloutState.load(state_path)
    print(f"  state: current_stage={state.current_stage}, ship_ts={state.ship_timestamps}")
    print()

    print("=" * 80)
    print("PHASE 3 — Stage 1 active. Now feed a losing streak. Kill switch trips.")
    print("=" * 80)
    # Already-shipped winners are pre-stage-1 (timestamps before stage1 ship_ts)
    # so they don't count toward the kill switch. Feed 12 losses with NEW timestamps.
    pre_stage1 = [winner(i) for i in range(10)]  # historical (won't be re-counted)
    stage1_losers = [loser(i) for i in range(12)]  # timestamps "2026-06-01T..." > ship_ts
    feed = pre_stage1 + stage1_losers
    print(fmt_decision(r.tick(feed), "12 losses"))
    state = RolloutState.load(state_path)
    print(f"  kill_tripped={state.kill_switch_tripped}, "
          f"reverted to stage={state.current_stage}")
    print()

    print("=" * 80)
    print("PHASE 4 — While killed, all advances are blocked.")
    print("=" * 80)
    print(fmt_decision(r.tick([winner(i) for i in range(20)]), "20 wins (blocked)"))
    print()

    print("=" * 80)
    print("PHASE 5 — Operator clears kill switch. Bot can advance again.")
    print("=" * 80)
    r.clear_kill_switch()
    state = RolloutState.load(state_path)
    print(f"  state: kill_tripped={state.kill_switch_tripped}")
    clock[0] = 1_001_000.0
    # New trades after kill clear (timestamps still > current stage 0's lack of ship_ts → all count)
    print(fmt_decision(r.tick([winner(i) for i in range(10)]), "10 wins post-clear"))
    clock[0] = 1_001_400.0
    print(fmt_decision(r.tick([winner(i) for i in range(10)]), "+400s (expired)"))

    state = RolloutState.load(state_path)
    print(f"  final stage={state.current_stage}")
    print()
    print(f"State file at: {state_path}")
    print("Done.")


if __name__ == "__main__":
    main()
