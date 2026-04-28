"""Trade-outcome metrics: win-rate, EV/$, total PnL.

A TradeOutcome is the minimal shape needed to evaluate stage-rollout gates:
  - pnl: realized profit/loss in account currency (USD, USDC, pUSD, etc.)
  - size: capital allocated to the trade (same unit as pnl)
  - timestamp: ISO-8601 UTC string

Anything else (token IDs, market metadata) is irrelevant to rollout decisions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence


@dataclass(frozen=True)
class TradeOutcome:
    pnl: float
    size: float
    timestamp: str  # ISO-8601 UTC
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Metrics:
    n: int
    wins: int
    win_rate: float          # 0.0–1.0 (NOT percent — Stage gates use 0–1)
    total_pnl: float
    total_size: float
    ev_per_dollar: float     # pnl/size, 0.04 = 4¢ per $1 (matches typical bot scale)


def compute_metrics(trades: Sequence[TradeOutcome]) -> Metrics:
    """Compute aggregate metrics for a sequence of TradeOutcome.

    Empty sequences return zeroed metrics — callers should check `n` before
    making rollout decisions.
    """
    n = len(trades)
    if n == 0:
        return Metrics(n=0, wins=0, win_rate=0.0, total_pnl=0.0, total_size=0.0, ev_per_dollar=0.0)

    wins = sum(1 for t in trades if t.pnl > 0)
    total_pnl = sum(t.pnl for t in trades)
    total_size = sum(t.size for t in trades)
    ev = (total_pnl / total_size) if total_size > 0 else 0.0
    return Metrics(
        n=n,
        wins=wins,
        win_rate=wins / n,
        total_pnl=total_pnl,
        total_size=total_size,
        ev_per_dollar=ev,
    )
