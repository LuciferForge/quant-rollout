# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-04-28

### Added
- Initial public release.
- `Stage`, `Gate`, `KillSwitch`, `Rollout` primitives.
- Pure decision logic — `Rollout.tick(trades)` returns a `RolloutDecision`; caller applies side effects.
- Persistent state via JSON file (configurable path).
- Veto window (configurable seconds; 0 disables for pure auto-advance).
- Kill switch evaluates trades scoped to the current stage (post-ship_ts only).
- 26 tests including end-to-end state machine simulation walking through:
  - Stage 0 → 1 advance with gate met
  - Veto window opens, expires, applies advance
  - Veto operator-cancels and re-fires
  - Kill switch trips on losing streak, auto-reverts to stage 0
  - Operator clears kill, system resumes
  - State persists across process restarts
- Pure stdlib core, zero runtime dependencies.
- MIT license.

[0.1.0]: https://github.com/LuciferForge/quant-rollout/releases/tag/v0.1.0
