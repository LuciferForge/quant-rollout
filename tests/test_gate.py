from quant_rollout import Gate, TradeOutcome, compute_metrics


def _t(pnl, size=5.0, ts="2026-04-01T00:00:00+00:00"):
    return TradeOutcome(pnl=pnl, size=size, timestamp=ts)


def test_empty_gate_always_passes():
    g = Gate()
    m = compute_metrics([_t(1.0), _t(-1.0)])
    passes, reason = g.evaluate(m)
    assert passes is True
    assert reason == "ok"


def test_min_n_blocks():
    g = Gate(min_n=10)
    m = compute_metrics([_t(1.0)])  # n=1
    passes, reason = g.evaluate(m)
    assert passes is False
    assert "n=1" in reason and "10" in reason


def test_min_win_rate_blocks():
    g = Gate(min_win_rate=0.80)
    # 50% WR
    m = compute_metrics([_t(1.0), _t(-1.0)])
    passes, reason = g.evaluate(m)
    assert passes is False
    assert "win_rate" in reason


def test_min_ev_blocks():
    g = Gate(min_ev_per_dollar=0.10)
    # ev = 0.5 / 10 = 0.05 → below 0.10 floor
    m = compute_metrics([_t(1.0, size=5.0), _t(-0.5, size=5.0)])
    passes, reason = g.evaluate(m)
    assert passes is False
    assert "ev_per_dollar" in reason


def test_min_total_pnl_blocks():
    g = Gate(min_total_pnl=10.0)
    m = compute_metrics([_t(1.0), _t(2.0)])
    passes, reason = g.evaluate(m)
    assert passes is False
    assert "total_pnl" in reason


def test_min_days_after_prev_blocks():
    g = Gate(min_days_after_prev=14.0)
    m = compute_metrics([_t(1.0)])
    passes, reason = g.evaluate(m, days_since_prev=3.0)
    assert passes is False
    assert "days_since_prev" in reason


def test_all_constraints_pass():
    g = Gate(
        min_n=2,
        min_win_rate=0.50,
        min_ev_per_dollar=0.05,
        min_total_pnl=0.5,
        min_days_after_prev=1.0,
    )
    m = compute_metrics([_t(1.0), _t(2.0), _t(-1.0)])  # n=3, WR=0.667, ev=0.133
    passes, reason = g.evaluate(m, days_since_prev=2.0)
    assert passes is True
