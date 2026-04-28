from quant_rollout import KillSwitch, TradeOutcome


def _t(pnl, size=5.0, ts="2026-04-01T00:00:00+00:00"):
    return TradeOutcome(pnl=pnl, size=size, timestamp=ts)


def test_no_trip_under_lookback():
    """Don't trip if we have fewer trades than the lookback window."""
    ks = KillSwitch(wr_lookback=30, wr_threshold=0.60, ev_lookback=100, ev_threshold=0.025)
    # 5 trades, all losses — would trigger if lookback were lower
    trades = [_t(-1.0)] * 5
    tripped, _ = ks.evaluate(trades)
    assert tripped is False


def test_trip_on_low_wr():
    ks = KillSwitch(wr_lookback=10, wr_threshold=0.60, ev_lookback=100, ev_threshold=-1.0)
    # 10 trades with 3 wins → 30% WR < 60% threshold
    trades = ([_t(1.0)] * 3) + ([_t(-1.0)] * 7)
    tripped, reason = ks.evaluate(trades)
    assert tripped is True
    assert "win_rate" in reason


def test_trip_on_low_ev():
    # WR threshold disabled (0.0), but EV threshold high
    ks = KillSwitch(wr_lookback=10, wr_threshold=0.0, ev_lookback=20, ev_threshold=0.10)
    # 20 trades at $0.01 PnL each, $5 size each → ev = 0.002 per dollar
    trades = [_t(0.01)] * 20
    tripped, reason = ks.evaluate(trades)
    assert tripped is True
    assert "ev_per_dollar" in reason


def test_dont_trip_when_healthy():
    ks = KillSwitch(wr_lookback=10, wr_threshold=0.60, ev_lookback=20, ev_threshold=0.05)
    # 20 trades, 16 winners at $1, 4 losers at $0.50 → WR 80%, ev=14/100=0.14
    trades = ([_t(1.0)] * 16) + ([_t(-0.5)] * 4)
    tripped, _ = ks.evaluate(trades)
    assert tripped is False
