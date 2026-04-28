from quant_rollout import TradeOutcome, compute_metrics


def _t(pnl, size=5.0, ts="2026-04-01T00:00:00+00:00"):
    return TradeOutcome(pnl=pnl, size=size, timestamp=ts)


def test_empty_returns_zeros():
    m = compute_metrics([])
    assert m.n == 0
    assert m.win_rate == 0.0
    assert m.ev_per_dollar == 0.0


def test_basic():
    trades = [_t(1.0), _t(2.0), _t(-1.0), _t(0.5)]
    m = compute_metrics(trades)
    assert m.n == 4
    assert m.wins == 3  # 1, 2, 0.5 are wins; -1 is a loss
    assert abs(m.win_rate - 0.75) < 1e-9
    assert m.total_pnl == 2.5
    assert m.total_size == 20.0
    assert abs(m.ev_per_dollar - 0.125) < 1e-9


def test_zero_size_safe():
    trades = [_t(1.0, size=0.0)]
    m = compute_metrics(trades)
    # Doesn't divide by zero
    assert m.ev_per_dollar == 0.0


def test_zero_pnl_not_a_win():
    trades = [_t(0.0)]
    m = compute_metrics(trades)
    # pnl > 0 is win; 0 is not a win
    assert m.wins == 0
    assert m.win_rate == 0.0
