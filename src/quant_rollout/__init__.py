"""quant-rollout — staged-deployment toolkit for live trading bots.

Public API:
    from quant_rollout import (
        Stage, Gate, KillSwitch, Rollout,
        TradeOutcome, RolloutDecision,
        compute_metrics,
    )

See README.md for usage.
"""
from quant_rollout.core import (
    Stage,
    Gate,
    KillSwitch,
    Rollout,
    RolloutDecision,
    RolloutAction,
)
from quant_rollout.metrics import TradeOutcome, compute_metrics

__version__ = "0.1.0"
__all__ = [
    "Stage",
    "Gate",
    "KillSwitch",
    "Rollout",
    "RolloutDecision",
    "RolloutAction",
    "TradeOutcome",
    "compute_metrics",
    "__version__",
]
