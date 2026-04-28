# quant-rollout

[![PyPI](https://img.shields.io/pypi/v/quant-rollout.svg)](https://pypi.org/project/quant-rollout/)
[![Python](https://img.shields.io/pypi/pyversions/quant-rollout.svg)](https://pypi.org/project/quant-rollout/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**Staged-deployment toolkit for live trading bots — gates, kill switch, veto window, persistent state machine.**

You changed a bot parameter. Did it actually help, or are you about to lose money? `quant-rollout` gives you the same canary → 10% → 50% → 100% rollout pattern that web teams use for product changes — adapted for trading bots, with a kill switch that auto-reverts on losing streaks.

## Why this exists

I rolled out 3 parameter changes on my Polymarket bot in 14 days. Each one needed:
- A gate: "advance to next stage iff WR ≥ 70% on N trades"
- A kill switch: "revert immediately if WR drops below 50% on the last 30 trades"
- A veto window: "30 minutes for me to abort before auto-advance"
- Persistent state: "if the bot crashes, remember which stage we're on"
- Pure decision logic: "let me unit-test this without launching a real bot"

I built it for my bot. Now it's a library you can drop into yours.

## Install

```bash
pip install quant-rollout
```

Zero runtime dependencies. Pure stdlib. Python ≥ 3.9.

## 60-second integration

```python
from quant_rollout import (
    Stage, Gate, KillSwitch, Rollout,
    RolloutAction, TradeOutcome,
)
from pathlib import Path

# Define your stages.
stages = [
    Stage(num=0, params={"max_entry_price": 0.30}, name="baseline"),
    Stage(
        num=1,
        params={"max_entry_price": 0.50},
        gate=Gate(min_n=50, min_win_rate=0.60, min_ev_per_dollar=0.03),
        name="canary",
    ),
    Stage(
        num=2,
        params={"max_entry_price": 0.50, "position_size": 10.0},
        gate=Gate(min_n=150, min_days_after_prev=14, min_ev_per_dollar=0.025),
        name="full_size",
    ),
]

# Kill switch — trips on a losing streak, reverts to stage 0.
kill_switch = KillSwitch(
    wr_lookback=30, wr_threshold=0.55,
    ev_lookback=100, ev_threshold=-0.01,
)

rollout = Rollout(
    stages=stages,
    kill_switch=kill_switch,
    state_path=Path("~/.bot/rollout_state.json").expanduser(),
    veto_window_seconds=1800,  # 30 min
)

# Once per minute (cron, scheduler, or your bot's main loop):
trades = my_bot.recent_closed_trades()  # → list[TradeOutcome]
decision = rollout.tick(trades)

if decision.action == RolloutAction.ADVANCE or decision.action == RolloutAction.VETO_EXPIRED:
    apply_config(stages[decision.to_stage].params)

elif decision.action == RolloutAction.KILL_TRIPPED:
    apply_config(stages[0].params)
    send_alert(f"KILL: {decision.reason}")

elif decision.action == RolloutAction.VETO_OPEN:
    send_alert(f"Stage advance pending. Run `rollout.veto_pending_advance()` to abort.")
```

## How it works

`Rollout.tick(trades)` runs through this state machine on every call:

```
                         ┌─────────────┐
   ─────────────────►   │  Kill check  │  ──── tripped ──►  KILL_TRIPPED, revert to stage 0
                         └──────┬──────┘
                                │ healthy
                                ▼
                         ┌─────────────┐
                         │ Veto window │  ──── still open ──►  VETO_OPEN
                         │   active?   │  ──── expired ─────►  VETO_EXPIRED, advance applied
                         └──────┬──────┘
                                │ no window
                                ▼
                         ┌─────────────┐
                         │  Next gate  │  ──── not yet met ──►  NOOP
                         │   passes?   │
                         └──────┬──────┘
                                │ yes
                                ▼
                         Open veto window
                         (or advance immediately if veto disabled)
```

The decision is returned to your code. The library does not perform side effects — your bot does the config swap, sends the alert, etc. This makes the whole thing trivially testable.

## What's in the box

| Class | Role |
|-------|------|
| `Stage(num, params, gate, name)` | A parameter set with optional advance gate. |
| `Gate(min_n, min_win_rate, min_ev_per_dollar, min_total_pnl, min_days_after_prev)` | Pass/fail criteria. All optional — Gate() is "always passes". |
| `KillSwitch(wr_lookback, wr_threshold, ev_lookback, ev_threshold)` | Trips on EITHER a low-WR streak OR a low-EV streak. |
| `Rollout(stages, kill_switch, state_path, veto_window_seconds)` | The orchestrator. `.tick(trades)` returns a `RolloutDecision`. |
| `TradeOutcome(pnl, size, timestamp, metadata)` | Minimal trade shape. Anything else (token IDs, market) is ignored. |
| `RolloutDecision(action, from_stage, to_stage, reason, metrics, veto_deadline_unix)` | What the caller acts on. |

`RolloutAction` enum values: `NOOP`, `KILL_TRIPPED`, `ADVANCE`, `VETO_OPEN`, `VETO_EXPIRED`.

## End-to-end demo

The demo walks the state machine through all 5 transitions in ~30 lines of output:

```bash
python examples/simulate_rollout.py
```

Sample output:
```
PHASE 1 — Stage 0. Need 10 trades + 60% WR + 4¢/$ to advance.
  [    5 wins] action=noop          n=  5 WR=100.0% ev/$=+40.0¢   reason=gate not yet met: n=5 < 10
  [   10 wins] action=veto_open     n= 10 WR=100.0% ev/$=+40.0¢   reason=gate passed; veto window opened

PHASE 3 — Stage 1 active. 12 losses → kill trips.
  [ 12 losses] action=kill          n= 12 WR=  0.0% ev/$=-60.0¢   reason=win_rate(10)=0.000 < 0.500
  kill_tripped=True, reverted to stage=0
```

## Design choices that matter

**Pure decision logic. No side effects.** The library returns decisions; your code applies them. This means:
- Unit tests run in milliseconds (no Telegram, no config files, no actual bot).
- You can plug ANY notification system (Slack, Discord, Telegram, email, none).
- Your "config swap" can be anything — JSON file, env-var, database, in-memory.

**Kill switch evaluates trades since current stage's ship_ts.** When the rollout advances from stage 0 → 1, the kill switch starts measuring stage 1 performance. It does NOT use stage 0 history — that data was generated under different parameters and isn't relevant for evaluating stage 1.

**Veto window is the safety belt.** Auto-advance is great until you're trading from your phone at the gym and your bot ships a buggy config. The veto window gives you 30 minutes (configurable) to call `rollout.veto_pending_advance()` from anywhere. Disable with `veto_window_seconds=0` if you want pure auto.

**State is a single JSON file.** Easy to inspect (`cat ~/.bot/rollout_state.json`), easy to back up, easy to reset (`rm`). No DB. No service. No cloud.

## Testing your own setup

Drop this into your test suite to assert your rollout config behaves as expected:

```python
from quant_rollout import Rollout, TradeOutcome, RolloutAction

def test_my_rollout_kills_on_bad_streak(tmp_path):
    rollout = my_bot.build_rollout(state_path=tmp_path / "state.json")

    # Simulate 50 winners → advance to stage 1
    winners = [TradeOutcome(2.0, 5.0, f"2026-05-01T00:{i:02d}:00+00:00") for i in range(50)]
    rollout.tick(winners)

    # Simulate 20 post-stage-1 losers → should trip kill
    losers = winners + [TradeOutcome(-3.0, 5.0, f"2026-06-01T{i:02d}:00:00+00:00") for i in range(20)]
    decision = rollout.tick(losers)
    assert decision.action == RolloutAction.KILL_TRIPPED
```

26 tests in this repo prove the same logic works for the library itself. `pytest tests/` to run them.

## When to use this

Good fit:
- A live trading bot where parameter changes are a regular activity.
- You want auto-advance, but with a human-veto safety net.
- You don't want to spin up a deployment platform for a single bot.

Bad fit:
- High-frequency strategies where decisions per second matter (this is per-tick, intended for ~minute granularity).
- Multi-bot orchestration (use one Rollout per bot — they're independent).
- A/B testing two strategies in parallel (different problem — see roadmap).

## Roadmap

- v0.2 — Multi-arm rollout (run two parameter sets in parallel, route 50/50, compare outcomes).
- v0.3 — Time-based gates (e.g., "advance only during weekday business hours").
- v0.4 — Persistent ship history with full audit trail.
- v0.5 — Optional Streamlit dashboard for `state.json`.

## License

MIT.

## About the author

Built by [LuciferForge](https://github.com/LuciferForge), running a [public-audited Polymarket trading bot](https://github.com/LuciferForge/polymarket-crash-bot) (302 closed trades, 79.8% WR). I extracted this from the bot's own `stage_tracker` engine after running 3 successful parameter rollouts in 14 days. Other projects:
- [polymarket-mcp](https://github.com/LuciferForge/polymarket-mcp) — MCP server for Polymarket
- [pnl-truthteller](https://github.com/LuciferForge/pnl-truthteller) — slippage audit tool
- [cross-signal-data](https://github.com/LuciferForge/cross-signal-data) — labeled crash-recovery dataset
- [polymarket-v2-migration](https://github.com/LuciferForge/polymarket-v2-migration) — V1→V2 cookbook
