"""End-to-end rollout state machine simulation.

Walks the rollout through realistic scenarios with a controlled clock and
trade feed, verifying the decision sequence matches expectations.
"""
import tempfile
from pathlib import Path

import pytest

from quant_rollout import (
    Stage, Gate, KillSwitch, Rollout,
    RolloutAction, TradeOutcome,
)
from quant_rollout.core import RolloutState


def _t(pnl, size=5.0, ts="2026-04-01T00:00:00+00:00"):
    return TradeOutcome(pnl=pnl, size=size, timestamp=ts)


def _winner(i, ship_ts="2026-04-01T00:00:00+00:00"):
    return TradeOutcome(pnl=2.0, size=5.0, timestamp=f"2026-05-01T00:00:{i:02d}+00:00")


def _loser(i, ship_ts="2026-04-01T00:00:00+00:00"):
    return TradeOutcome(pnl=-3.0, size=5.0, timestamp=f"2026-05-01T00:00:{i:02d}+00:00")


@pytest.fixture
def tmp_state(tmp_path):
    return tmp_path / "rollout_state.json"


def _build_rollout(tmp_state, *, veto_window_seconds=0, clock_value=[1000.0]):
    """Build a Rollout with a controllable clock for deterministic tests."""
    stages = [
        Stage(num=0, params={"max_entry": 0.30}, name="baseline"),
        Stage(
            num=1,
            params={"max_entry": 0.50},
            gate=Gate(min_n=10, min_win_rate=0.60, min_ev_per_dollar=0.04),
            name="stage_1",
        ),
        Stage(
            num=2,
            params={"max_entry": 0.70, "position_size": 10.0},
            gate=Gate(min_n=20, min_win_rate=0.70, min_ev_per_dollar=0.05),
            name="stage_2",
        ),
    ]
    ks = KillSwitch(
        wr_lookback=10,
        wr_threshold=0.40,
        ev_lookback=20,
        ev_threshold=-0.05,
    )

    return Rollout(
        stages=stages,
        kill_switch=ks,
        state_path=tmp_state,
        veto_window_seconds=veto_window_seconds,
        clock_now_unix=lambda: clock_value[0],
        clock_now_iso=lambda: "2026-05-01T12:00:00+00:00",
    )


def test_initial_state_is_baseline_noop(tmp_state):
    r = _build_rollout(tmp_state)
    decision = r.tick([])
    assert decision.action == RolloutAction.NOOP
    assert decision.from_stage == 0
    assert decision.to_stage == 0


def test_advance_when_gate_met_no_veto(tmp_state):
    r = _build_rollout(tmp_state, veto_window_seconds=0)
    # 10 winners → WR 100%, ev = (10*2)/(10*5) = 0.4 → exceeds gate
    trades = [_winner(i) for i in range(10)]
    decision = r.tick(trades)
    assert decision.action == RolloutAction.ADVANCE
    assert decision.from_stage == 0
    assert decision.to_stage == 1


def test_dont_advance_when_gate_not_met(tmp_state):
    r = _build_rollout(tmp_state, veto_window_seconds=0)
    trades = [_winner(i) for i in range(5)]  # only 5 trades, gate needs 10
    decision = r.tick(trades)
    assert decision.action == RolloutAction.NOOP
    assert "n=5" in decision.reason


def test_veto_window_opens_then_expires(tmp_state):
    clock = [1000.0]
    r = _build_rollout(tmp_state, veto_window_seconds=300, clock_value=clock)

    trades = [_winner(i) for i in range(10)]

    # Tick 1: opens veto window
    d1 = r.tick(trades)
    assert d1.action == RolloutAction.VETO_OPEN
    assert d1.veto_deadline_unix == 1300.0

    # Tick 2: still within window
    clock[0] = 1100.0
    d2 = r.tick(trades)
    assert d2.action == RolloutAction.VETO_OPEN

    # Tick 3: window expired → advance applied
    clock[0] = 1400.0
    d3 = r.tick(trades)
    assert d3.action == RolloutAction.VETO_EXPIRED
    assert d3.from_stage == 0
    assert d3.to_stage == 1


def test_veto_aborts_pending_advance(tmp_state):
    clock = [1000.0]
    r = _build_rollout(tmp_state, veto_window_seconds=300, clock_value=clock)
    trades = [_winner(i) for i in range(10)]

    # Open veto window
    r.tick(trades)
    state = RolloutState.load(tmp_state)
    assert state.advance_pending_to == 1

    # Operator vetoes
    r.veto_pending_advance(note="not now")
    state = RolloutState.load(tmp_state)
    assert state.advance_pending_to is None

    # Next tick after veto: gate still met, should re-open veto window
    clock[0] = 1500.0
    d = r.tick(trades)
    assert d.action == RolloutAction.VETO_OPEN  # re-fires


def test_kill_switch_trips_and_reverts(tmp_state):
    r = _build_rollout(tmp_state, veto_window_seconds=0)
    # First, advance to stage 1
    winners = [_winner(i) for i in range(10)]
    r.tick(winners)
    state = RolloutState.load(tmp_state)
    assert state.current_stage == 1
    # Stage 1 ship_ts is "2026-05-01T12:00:00+00:00" (mock clock_now_iso)

    # Feed losers with timestamps AFTER stage 1 ship — these count as stage-1 trades.
    # Need >= wr_lookback (10) at WR=0% to trip kill (threshold 40%).
    losers = winners + [
        TradeOutcome(pnl=-3.0, size=5.0, timestamp=f"2026-06-01T00:00:{i:02d}+00:00")
        for i in range(10)
    ]
    decision = r.tick(losers)
    assert decision.action == RolloutAction.KILL_TRIPPED
    assert decision.to_stage == 0  # reverted

    # Subsequent tick: locked at stage 0 with kill flag set
    state = RolloutState.load(tmp_state)
    assert state.kill_switch_tripped is True
    assert state.current_stage == 0

    decision_after = r.tick(losers)
    assert decision_after.action == RolloutAction.NOOP
    assert "kill switch tripped" in decision_after.reason


def test_clear_kill_switch_resumes(tmp_state):
    r = _build_rollout(tmp_state, veto_window_seconds=0)
    # Trip kill switch
    losers = [_loser(i) for i in range(15)]
    r.tick(losers)
    state = RolloutState.load(tmp_state)
    assert state.kill_switch_tripped is True

    # Operator clears
    r.clear_kill_switch()
    state = RolloutState.load(tmp_state)
    assert state.kill_switch_tripped is False

    # Next tick is normal noop (we're at stage 0, no qualifying trades yet)
    d = r.tick([_winner(i) for i in range(5)])
    assert d.action == RolloutAction.NOOP


def test_state_persists_across_restarts(tmp_state):
    r1 = _build_rollout(tmp_state)
    r1.tick([_winner(i) for i in range(10)])

    # Simulate process restart — fresh Rollout instance, same state file
    r2 = _build_rollout(tmp_state)
    state = RolloutState.load(tmp_state)
    assert state.current_stage == 1


def test_full_happy_path_through_all_stages(tmp_state):
    """Walk from stage 0 → 1 → 2 with veto disabled."""
    r = _build_rollout(tmp_state, veto_window_seconds=0)

    # Stage 0 → 1
    s1_trades = [_winner(i) for i in range(10)]
    d1 = r.tick(s1_trades)
    assert d1.action == RolloutAction.ADVANCE
    assert d1.to_stage == 1

    # Now we're on stage 1. The gate for stage 2 needs n>=20 with WR >= 70%
    # and ev >= 0.05. But trades_since_current_stage filters by stage's ship_ts.
    # The current stage 1 ship_ts will be "2026-05-01T12:00:00+00:00" (from clock_now_iso).
    # So we need 20 trades AFTER that ts.
    s2_trades = [
        TradeOutcome(pnl=2.0, size=5.0, timestamp=f"2026-06-01T00:00:{i:02d}+00:00")
        for i in range(20)
    ]
    d2 = r.tick(s2_trades)
    assert d2.action == RolloutAction.ADVANCE
    assert d2.from_stage == 1
    assert d2.to_stage == 2


def test_no_further_stage_returns_noop(tmp_state):
    r = _build_rollout(tmp_state, veto_window_seconds=0)
    # Force state to be at the final stage
    state = RolloutState(current_stage=2)
    state.save(tmp_state)
    d = r.tick([])
    assert d.action == RolloutAction.NOOP
    assert "no further stage" in d.reason


def test_reset_wipes_state(tmp_state):
    r = _build_rollout(tmp_state, veto_window_seconds=0)
    r.tick([_winner(i) for i in range(10)])
    state = RolloutState.load(tmp_state)
    assert state.current_stage == 1

    r.reset()
    state = RolloutState.load(tmp_state)
    assert state.current_stage == 0
    assert state.kill_switch_tripped is False
    assert state.ship_timestamps == {}
