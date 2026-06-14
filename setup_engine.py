from pathlib import Path

from trade_simulator import SetupProposal


print(f"DEBUG setup_engine.py imported from: {Path(__file__).resolve()}")


FINAL_STRATEGY_FILTERS = {
    "max_distance_ema20_pct": None,
    "max_distance_ema50_pct": None,
    "min_trend_strength_pct": None,
    "max_trend_strength_pct": None,
    "min_volatility_20d_pct": None,
    "max_volatility_20d_pct": 3.0,
    "min_atr14_pct": None,
    "max_atr14_pct": None,
    "exclude_tickers": ["TSLA"],
}


def generate_setup(history, ticker, filters=None):

    if len(history) < 250:
        return None

    last = float(history["Close"].iloc[-1])

    ema20 = float(history["Close"].ewm(span=20).mean().iloc[-1])
    ema50 = float(history["Close"].ewm(span=50).mean().iloc[-1])
    ema200 = float(history["Close"].ewm(span=200).mean().iloc[-1])

    if not (last > ema20 > ema50 > ema200):
        return None

    features = _calculate_features(
        history=history,
        last=last,
        ema20=ema20,
        ema50=ema50,
        ema200=ema200,
    )

    if _is_filtered_out(ticker, features, filters):
        return None

    zone_top = float(ema20)
    zone_bottom = float(ema50)

    entry = (zone_top + zone_bottom) / 2

    risk = entry * 0.05

    stop = entry - risk

    target1 = entry + (risk * 2)

    target2 = entry + (risk * 4)

    return SetupProposal(
        date=str(history.index[-1].date()),
        ticker=ticker,
        setup_type="EMA Pullback",
        score=70,
        sm_score=5,
        flow_score=0.0,
        zone_bottom=zone_bottom,
        zone_top=zone_top,
        entry_price=entry,
        stop_loss=stop,
        target1=target1,
        target2=target2,
        rr_zone=2.0,
        distance_ema20_pct=features["distance_ema20_pct"],
        distance_ema50_pct=features["distance_ema50_pct"],
        trend_strength_pct=features["trend_strength_pct"],
        volatility_20d_pct=features["volatility_20d_pct"],
        atr14=features["atr14"],
        atr14_pct=features["atr14_pct"],
    )


print(f"DEBUG generate_setup defined in setup_engine: {callable(generate_setup)}")


def _calculate_features(history, last, ema20, ema50, ema200):
    high = history["High"]
    low = history["Low"]
    close = history["Close"]
    previous_close = close.shift(1)

    true_range = (
        (high - low)
        .to_frame("high_low")
        .join((high - previous_close).abs().rename("high_close"))
        .join((low - previous_close).abs().rename("low_close"))
        .max(axis=1)
    )

    atr14 = float(true_range.rolling(14).mean().iloc[-1])
    volatility_20d_pct = float(close.pct_change().rolling(20).std().iloc[-1] * 100)

    return {
        "distance_ema20_pct": ((last / ema20) - 1) * 100,
        "distance_ema50_pct": ((last / ema50) - 1) * 100,
        "trend_strength_pct": ((ema20 / ema200) - 1) * 100,
        "volatility_20d_pct": volatility_20d_pct,
        "atr14": atr14,
        "atr14_pct": (atr14 / last) * 100 if last else 0.0,
    }


def _is_filtered_out(ticker, features, filters):
    if not filters:
        return False

    excluded = filters.get("exclude_tickers", [])
    if ticker in excluded:
        return True

    checks = [
        ("distance_ema20_pct", "min_distance_ema20_pct", "max_distance_ema20_pct"),
        ("distance_ema50_pct", "min_distance_ema50_pct", "max_distance_ema50_pct"),
        ("trend_strength_pct", "min_trend_strength_pct", "max_trend_strength_pct"),
        ("volatility_20d_pct", "min_volatility_20d_pct", "max_volatility_20d_pct"),
        ("atr14_pct", "min_atr14_pct", "max_atr14_pct"),
    ]

    for feature_name, min_key, max_key in checks:
        value = features[feature_name]
        min_value = filters.get(min_key)
        max_value = filters.get(max_key)

        if min_value is not None and value < min_value:
            return True

        if max_value is not None and value > max_value:
            return True

    return False
