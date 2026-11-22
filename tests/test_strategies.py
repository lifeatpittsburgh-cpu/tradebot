import pytest

from tradebot.strategies import REGISTRY, get_strategy


@pytest.mark.parametrize("name", list(REGISTRY))
def test_no_lookahead(name, random_walk):
    """Signals up to bar t must not change when later bars are appended."""
    strat = get_strategy(name)
    full = strat.generate(random_walk)["signal"]
    cut = 600
    partial = strat.generate(random_walk.iloc[:cut])["signal"]
    assert (full.iloc[:cut].values == partial.values).all()


@pytest.mark.parametrize("name", list(REGISTRY))
def test_signal_values(name, random_walk):
    out = get_strategy(name).generate(random_walk)
    assert set(out["signal"].unique()) <= {-1, 0, 1}
    assert (out.loc[out["signal"] != 0, "reason"] != "").all()


def test_sma_crossover_buys_after_reversal(down_then_up):
    out = get_strategy("sma_crossover", use_trend_filter=False).generate(down_then_up)
    buys = out.index[out["signal"] == 1]
    assert len(buys) == 1
    assert buys[0] > down_then_up.index[250]  # after the turn


def test_trend_filter_blocks_buys_in_downtrend(down_then_up):
    first_half = down_then_up.iloc[:260]
    out = get_strategy("sma_crossover").generate(first_half)
    assert (out["signal"] != 1).all()


def test_unknown_param_rejected():
    with pytest.raises(ValueError):
        get_strategy("sma_crossover", fastt=10)
    with pytest.raises(ValueError):
        get_strategy("nope")
