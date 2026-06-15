"""Smoke testy pro čisté (bezsíťové) funkce z akciovygenius.py.

Spuštění:
    pytest tests/            # nebo:  python -m pytest tests/test_pure.py
"""
import os
import sys

import pandas as pd

# Zpřístupni kořen repa pro import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import akciovygenius as ag  # noqa: E402


def test_fmt_usd():
    assert ag.fmt_usd(500) == "$500"
    assert ag.fmt_usd(2000) == "$2K"
    assert ag.fmt_usd(2_500_000) == "$2.50M"


def test_moneyness_atm_and_guard():
    # spot <= 0 → bezpečná výchozí hodnota
    assert ag.moneyness(100, 0, "call") == ("ATM", 1.0)
    # strike == spot → ATM
    label, weight = ag.moneyness(100, 100, "call")
    assert label == "ATM"
    assert weight == 1.00
    # ITM call (spot výrazně nad strike)
    label_itm, _ = ag.moneyness(90, 100, "call")
    assert label_itm == "ITM"


def test_aggression_score_bounds():
    assert ag.aggression_score(2.0, 1.0, 2.0) == 1.0   # na asku
    assert ag.aggression_score(1.0, 1.0, 2.0) == 0.0   # na bidu
    assert ag.aggression_score(1.0, 0.0, 0.0) == 0.5   # neplatný spread → neutrál


def test_cluster_levels():
    assert ag.cluster_levels([], 1.0) == []
    points = [(0, 10.0), (1, 10.5), (2, 20.0)]
    clusters = ag.cluster_levels(points, tolerance=1.0)
    assert len(clusters) == 2
    # první cluster = dvě blízké úrovně kolem 10.25
    mean, count, members = clusters[0]
    assert count == 2
    assert 10.0 <= mean <= 10.5


def test_find_pivots_detects_peak():
    high = [1.0, 1.0, 1.0, 1.0, 5.0, 1.0, 1.0, 1.0, 1.0]
    low = [9.0, 9.0, 9.0, 9.0, 0.5, 9.0, 9.0, 9.0, 9.0]
    df = pd.DataFrame({"High": high, "Low": low})
    highs, lows = ag.find_pivots(df, window=2)
    assert any(abs(v - 5.0) < 1e-9 for _, v in highs)
    assert any(abs(v - 0.5) < 1e-9 for _, v in lows)


def test_compute_flow_score_empty():
    score, buckets, confidence = ag.compute_flow_score([])
    assert score == 0.0
    assert all(v == 0.0 for v in buckets.values())
    assert confidence == "🔴 Nízká"


def test_compute_flow_score_bullish():
    hits = [{"bscore_sum": 2, "wscore": 100.0, "opt_type": "call", "premium": 2_000_000}]
    score, buckets, confidence = ag.compute_flow_score(hits)
    assert score == 1.0                 # čistě bullish
    assert buckets["bull_call"] == 100.0
    assert confidence == "🟡 Střední"   # premium >= 1M


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
