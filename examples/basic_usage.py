#!/usr/bin/env python3
"""Minimal integration: how a real bot calls quant-rollout in production.

Run:
    python examples/basic_usage.py

This script writes to /tmp by default — replace with your actual paths.
"""
from pathlib import Path

from quant_rollout import (
    Stage, Gate, KillSwitch, Rollout,
    RolloutAction, TradeOutcome,
)


# 1. Define your stages.
stages = [
    Stage(num=0, params={"max_entry_price": 0.30, "position_size_usd": 5.0},
          name="baseline"),
    Stage(num=1, params={"max_entry_price": 0.50, "position_size_usd": 5.0},
          gate=Gate(min_n=50, min_win_rate=0.60, min_ev_per_dollar=0.03),
          name="canary"),
    Stage(num=2, params={"max_entry_price": 0.50, "position_size_usd": 10.0},
          gate=Gate(min_n=150, min_days_after_prev=14.0, min_ev_per_dollar=0.025),
          name="full_size"),
]

# 2. Configure the kill switch.
kill_switch = KillSwitch(
    wr_lookback=30, wr_threshold=0.55,
    ev_lookback=100, ev_threshold=-0.01,
)

# 3. Construct the Rollout. Veto window is 30 minutes by default.
rollout = Rollout(
    stages=stages,
    kill_switch=kill_switch,
    state_path=Path("/tmp/quant_rollout_state.json"),
    veto_window_seconds=1800,
)


# 4. In your bot or cron, fetch the latest closed-trade list.
def get_my_bot_trades() -> list[TradeOutcome]:
    """Replace with a query against your bot's DB / positions.json."""
    return [
        TradeOutcome(pnl=2.0, size=5.0, timestamp="2026-04-25T10:00:00+00:00"),
        TradeOutcome(pnl=-3.0, size=5.0, timestamp="2026-04-25T11:00:00+00:00"),
        # ... etc
    ]


# 5. Tick the rollout, act on the decision.
trades = get_my_bot_trades()
decision = rollout.tick(trades)

print(f"Action: {decision.action.value}")
print(f"Stage: {decision.from_stage} → {decision.to_stage}")
print(f"Reason: {decision.reason}")
print(f"Metrics: n={decision.metrics.n}, "
      f"WR={decision.metrics.win_rate*100:.1f}%, "
      f"ev/$={decision.metrics.ev_per_dollar*100:+.1f}¢")

# 6. Apply side effects based on the decision.
if decision.action == RolloutAction.ADVANCE or decision.action == RolloutAction.VETO_EXPIRED:
    new_stage = next(s for s in stages if s.num == decision.to_stage)
    print(f"-> Apply config swap: {new_stage.params}")
    # write_bot_config(new_stage.params)

elif decision.action == RolloutAction.KILL_TRIPPED:
    baseline = next(s for s in stages if s.num == 0)
    print(f"-> KILL: revert to baseline {baseline.params}")
    # write_bot_config(baseline.params)
    # send_telegram_alert(f"KILL: {decision.reason}")

elif decision.action == RolloutAction.VETO_OPEN:
    print(f"-> Veto window open until unix={decision.veto_deadline_unix}")
    # send_telegram_alert(f"Stage advance pending. /veto in next 30 min to abort.")

elif decision.action == RolloutAction.NOOP:
    pass  # nothing to do
