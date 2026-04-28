"""Core rollout state machine.

Three primitives:
  - Stage: a parameter set with optional advance Gate.
  - Gate: pass/fail criteria to advance from previous stage to this one.
  - KillSwitch: emergency revert criteria evaluated continuously.

A Rollout owns:
  - The list of Stages.
  - Persistent state (current stage, ship timestamps, kill flag) — JSON file.
  - A `tick(trades)` method that decides what to do based on a fresh trade list.

Side effects (config swap, Telegram veto, log) are user callbacks — the library
itself is pure decision logic.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Sequence

from quant_rollout.metrics import TradeOutcome, Metrics, compute_metrics


# ───────────────────────────────────────────────────────────────────
# Decision types
# ───────────────────────────────────────────────────────────────────


class RolloutAction(str, Enum):
    NOOP = "noop"               # nothing to do this tick
    KILL_TRIPPED = "kill"       # kill switch triggered — revert to baseline
    ADVANCE = "advance"         # gate passed — promote to next stage
    VETO_OPEN = "veto_open"     # advance pending; veto window open
    VETO_EXPIRED = "veto_expired"  # veto window expired without abort — apply now


@dataclass
class RolloutDecision:
    action: RolloutAction
    from_stage: int
    to_stage: int
    reason: str
    metrics: Metrics
    veto_deadline_unix: float | None = None


# ───────────────────────────────────────────────────────────────────
# Configuration types
# ───────────────────────────────────────────────────────────────────


@dataclass
class Gate:
    """Pass/fail criteria for stage advancement.

    All fields are optional — a Gate with no constraints always passes.
    A trade list satisfies the gate iff ALL specified constraints pass.
    """

    min_n: int = 0
    min_win_rate: float = 0.0           # 0.0–1.0
    min_ev_per_dollar: float = 0.0      # USD per USD (0.04 = 4¢ per $1)
    min_total_pnl: float | None = None  # absolute USD floor, optional
    min_days_after_prev: float = 0.0    # wall-clock days since previous stage shipped

    def evaluate(
        self, metrics: Metrics, *, days_since_prev: float | None = None
    ) -> tuple[bool, str]:
        """Return (passes, reason)."""
        if metrics.n < self.min_n:
            return False, f"n={metrics.n} < {self.min_n}"
        if metrics.win_rate < self.min_win_rate:
            return False, (
                f"win_rate={metrics.win_rate:.3f} < {self.min_win_rate:.3f}"
            )
        if metrics.ev_per_dollar < self.min_ev_per_dollar:
            return False, (
                f"ev_per_dollar={metrics.ev_per_dollar:.4f} < {self.min_ev_per_dollar:.4f}"
            )
        if self.min_total_pnl is not None and metrics.total_pnl < self.min_total_pnl:
            return False, (
                f"total_pnl=${metrics.total_pnl:.2f} < ${self.min_total_pnl:.2f}"
            )
        if self.min_days_after_prev > 0 and (days_since_prev or 0) < self.min_days_after_prev:
            return False, (
                f"days_since_prev={days_since_prev or 0:.1f} "
                f"< {self.min_days_after_prev:.1f}"
            )
        return True, "ok"


@dataclass
class KillSwitch:
    """Emergency-revert criteria. Evaluated on every tick.

    Trips if EITHER:
      - last `wr_lookback` trades have win_rate < `wr_threshold`, OR
      - last `ev_lookback` trades have ev_per_dollar < `ev_threshold`.
    """

    wr_lookback: int = 30
    wr_threshold: float = 0.60          # 60% WR floor
    ev_lookback: int = 100
    ev_threshold: float = 0.025         # 2.5¢/$ floor

    def evaluate(self, trades: Sequence[TradeOutcome]) -> tuple[bool, str | None]:
        """Return (tripped, reason). reason is None when not tripped."""
        if len(trades) >= self.wr_lookback:
            recent_wr = compute_metrics(trades[-self.wr_lookback:])
            if recent_wr.win_rate < self.wr_threshold:
                return True, (
                    f"win_rate({self.wr_lookback})={recent_wr.win_rate:.3f} "
                    f"< {self.wr_threshold:.3f}"
                )
        if len(trades) >= self.ev_lookback:
            recent_ev = compute_metrics(trades[-self.ev_lookback:])
            if recent_ev.ev_per_dollar < self.ev_threshold:
                return True, (
                    f"ev_per_dollar({self.ev_lookback})={recent_ev.ev_per_dollar:.4f} "
                    f"< {self.ev_threshold:.4f}"
                )
        return False, None


@dataclass
class Stage:
    """A named parameter configuration with optional advance gate.

    Stage 0 is the implicit baseline. The first user-defined stage typically
    advances from 0 → 1 once the bot has produced N trades.
    """

    num: int
    params: dict[str, Any]
    gate: Gate | None = None
    name: str = ""


# ───────────────────────────────────────────────────────────────────
# Persistent state
# ───────────────────────────────────────────────────────────────────


@dataclass
class RolloutState:
    current_stage: int = 0
    ship_timestamps: dict[str, str] = field(default_factory=dict)  # str(stage_num) -> iso
    kill_switch_tripped: bool = False
    kill_reason: str | None = None
    kill_timestamp: str | None = None
    advance_pending_to: int | None = None
    advance_veto_deadline_unix: float | None = None
    last_tick_unix: float | None = None
    last_decision_action: str | None = None

    @classmethod
    def load(cls, path: Path) -> "RolloutState":
        if not path.exists():
            return cls()
        try:
            return cls(**json.loads(path.read_text()))
        except (json.JSONDecodeError, TypeError):
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))


# ───────────────────────────────────────────────────────────────────
# Rollout — the orchestrator
# ───────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_unix() -> float:
    return datetime.now(timezone.utc).timestamp()


def _days_between(iso_ts: str | None) -> float:
    if not iso_ts:
        return 0.0
    try:
        t = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 86400
    except ValueError:
        return 0.0


@dataclass
class Rollout:
    """Staged deployment orchestrator.

    Construct once, call `tick(trades)` whenever new trade data is available.
    The Rollout consults the current state (loaded from disk) and the kill
    switch + stage gates, returns a RolloutDecision, and persists state.

    Side effects (config swap, Telegram, etc.) are returned as decisions —
    the caller acts on them. This keeps the library pure and testable.
    """

    stages: list[Stage]
    kill_switch: KillSwitch
    state_path: Path
    veto_window_seconds: int = 1800     # 30 min
    clock_now_unix: Callable[[], float] = field(default_factory=lambda: _now_unix)
    clock_now_iso: Callable[[], str] = field(default_factory=lambda: _now_iso)

    def __post_init__(self):
        self.stages = sorted(self.stages, key=lambda s: s.num)
        if not self.stages:
            raise ValueError("Rollout requires at least one Stage.")

    def _stage_by_num(self, n: int) -> Stage | None:
        for s in self.stages:
            if s.num == n:
                return s
        return None

    def current_stage(self, state: RolloutState) -> Stage | None:
        return self._stage_by_num(state.current_stage)

    def next_stage(self, state: RolloutState) -> Stage | None:
        for s in self.stages:
            if s.num > state.current_stage:
                return s
        return None

    def trades_since_current_stage(
        self, trades: Sequence[TradeOutcome], state: RolloutState
    ) -> list[TradeOutcome]:
        """Return only trades placed AFTER the current stage shipped.

        Stage 0 (baseline) has no ship_ts — all trades count.
        """
        ship_ts = state.ship_timestamps.get(str(state.current_stage))
        if not ship_ts:
            return list(trades)
        return [t for t in trades if t.timestamp >= ship_ts]

    def tick(self, trades: Sequence[TradeOutcome]) -> RolloutDecision:
        """Evaluate one rollout tick.

        Returns a RolloutDecision describing what action (if any) the caller
        should take. Persists state to disk before returning.
        """
        state = RolloutState.load(self.state_path)
        state.last_tick_unix = self.clock_now_unix()

        scoped_trades = self.trades_since_current_stage(trades, state)
        m = compute_metrics(scoped_trades)

        # 1. Kill switch — evaluated EVERY tick, even if already tripped (idempotent)
        if not state.kill_switch_tripped:
            tripped, reason = self.kill_switch.evaluate(scoped_trades)
            if tripped:
                state.kill_switch_tripped = True
                state.kill_reason = reason
                state.kill_timestamp = self.clock_now_iso()
                # On kill: cancel any pending advance
                state.advance_pending_to = None
                state.advance_veto_deadline_unix = None
                # Revert to baseline (stage 0) — caller swaps the config
                from_stage = state.current_stage
                state.current_stage = 0
                state.last_decision_action = RolloutAction.KILL_TRIPPED.value
                state.save(self.state_path)
                return RolloutDecision(
                    action=RolloutAction.KILL_TRIPPED,
                    from_stage=from_stage,
                    to_stage=0,
                    reason=reason or "kill switch tripped",
                    metrics=m,
                )

        # 2. If a veto window is open, check whether it has expired
        if state.advance_pending_to is not None:
            now = self.clock_now_unix()
            if now >= (state.advance_veto_deadline_unix or 0):
                # Veto window expired without intervention — apply
                target_stage = state.advance_pending_to
                from_stage = state.current_stage
                state.current_stage = target_stage
                state.ship_timestamps[str(target_stage)] = self.clock_now_iso()
                state.advance_pending_to = None
                state.advance_veto_deadline_unix = None
                state.last_decision_action = RolloutAction.VETO_EXPIRED.value
                state.save(self.state_path)
                return RolloutDecision(
                    action=RolloutAction.VETO_EXPIRED,
                    from_stage=from_stage,
                    to_stage=target_stage,
                    reason="veto window expired without abort",
                    metrics=m,
                )
            # Still within window — keep waiting
            state.last_decision_action = RolloutAction.VETO_OPEN.value
            state.save(self.state_path)
            return RolloutDecision(
                action=RolloutAction.VETO_OPEN,
                from_stage=state.current_stage,
                to_stage=state.advance_pending_to,
                reason="veto window still open",
                metrics=m,
                veto_deadline_unix=state.advance_veto_deadline_unix,
            )

        # 3. Check whether next stage's gate passes
        if state.kill_switch_tripped:
            # Locked out — caller must explicitly clear via clear_kill()
            state.last_decision_action = RolloutAction.NOOP.value
            state.save(self.state_path)
            return RolloutDecision(
                action=RolloutAction.NOOP,
                from_stage=state.current_stage,
                to_stage=state.current_stage,
                reason=f"kill switch tripped: {state.kill_reason}",
                metrics=m,
            )

        nxt = self.next_stage(state)
        if nxt is None or nxt.gate is None:
            # No further stage, or next stage has no gate (must be advanced manually)
            state.last_decision_action = RolloutAction.NOOP.value
            state.save(self.state_path)
            return RolloutDecision(
                action=RolloutAction.NOOP,
                from_stage=state.current_stage,
                to_stage=state.current_stage,
                reason=(
                    "no further stage" if nxt is None else "next stage has no gate"
                ),
                metrics=m,
            )

        prev_ship_ts = state.ship_timestamps.get(str(state.current_stage))
        days_since = _days_between(prev_ship_ts)
        passes, reason = nxt.gate.evaluate(m, days_since_prev=days_since)
        if not passes:
            state.last_decision_action = RolloutAction.NOOP.value
            state.save(self.state_path)
            return RolloutDecision(
                action=RolloutAction.NOOP,
                from_stage=state.current_stage,
                to_stage=state.current_stage,
                reason=f"gate not yet met: {reason}",
                metrics=m,
            )

        # Gate passed — open veto window OR advance immediately if veto disabled
        if self.veto_window_seconds <= 0:
            from_stage = state.current_stage
            state.current_stage = nxt.num
            state.ship_timestamps[str(nxt.num)] = self.clock_now_iso()
            state.last_decision_action = RolloutAction.ADVANCE.value
            state.save(self.state_path)
            return RolloutDecision(
                action=RolloutAction.ADVANCE,
                from_stage=from_stage,
                to_stage=nxt.num,
                reason=f"gate passed: {reason}",
                metrics=m,
            )

        deadline = self.clock_now_unix() + self.veto_window_seconds
        state.advance_pending_to = nxt.num
        state.advance_veto_deadline_unix = deadline
        state.last_decision_action = RolloutAction.VETO_OPEN.value
        state.save(self.state_path)
        return RolloutDecision(
            action=RolloutAction.VETO_OPEN,
            from_stage=state.current_stage,
            to_stage=nxt.num,
            reason=f"gate passed; veto window opened",
            metrics=m,
            veto_deadline_unix=deadline,
        )

    def veto_pending_advance(self, note: str = "") -> RolloutState:
        """Cancel an in-progress advance during the veto window."""
        state = RolloutState.load(self.state_path)
        state.advance_pending_to = None
        state.advance_veto_deadline_unix = None
        state.last_decision_action = "vetoed"
        state.save(self.state_path)
        return state

    def clear_kill_switch(self) -> RolloutState:
        """Operator-initiated reset after a kill — stays at stage 0 until next gate fires."""
        state = RolloutState.load(self.state_path)
        state.kill_switch_tripped = False
        state.kill_reason = None
        state.kill_timestamp = None
        state.save(self.state_path)
        return state

    def reset(self) -> RolloutState:
        """Wipe state — back to stage 0, no kill, no pending advance."""
        state = RolloutState()
        state.save(self.state_path)
        return state
