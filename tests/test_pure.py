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


def _snap(date, oi, premium):
    return {"date": date, "oi": oi, "volume": oi, "premium": float(premium),
            "wscore": 0.0, "bscore": 2}


def test_accum_empty_and_new():
    # prázdná historie → None
    assert ag._accum_from_history([]) is None
    # jeden den → "nový", ne akumulace
    new = ag._accum_from_history([_snap("2026-06-15", 1000, 500_000)])
    assert new["days"] == 1
    assert new["is_accum"] is False


def test_accum_detects_accumulation():
    hist = [_snap("2026-06-12", 1000, 400_000),
            _snap("2026-06-13", 1800, 700_000),
            _snap("2026-06-15", 2600, 1_200_000)]
    a = ag._accum_from_history(hist)
    assert a["is_accum"] is True
    assert a["days"] == 3
    assert abs(a["oi_growth"] - 2.6) < 1e-9
    assert abs(a["cum_premium"] - 2_300_000) < 1.0


def test_accum_detects_distribution():
    hist = [_snap("2026-06-12", 5000, 900_000), _snap("2026-06-15", 1500, 200_000)]
    a = ag._accum_from_history(hist)
    assert a["label"] == "🔴 Distribuce"
    assert a["is_accum"] is False


def test_genius_fuse_empty_is_neutral():
    # žádné pohledy → neutrál, nulové skóre, nízká jistota
    r = ag.genius_fuse({"ticker": "X"})
    assert r["score"] == 0
    assert "NEUTRÁLNÍ" in r["direction"]
    assert r["confidence"] == "🔴 Nízká"
    assert r["factors"] == []


def test_genius_fuse_agreement_amplifies():
    # technika i flow bullish a ve shodě → bullish, slušné skóre
    lenses = {
        "ticker": "AAPL", "last": 200.0,
        "tech": {"setup_type": "🚀 Momentum Breakout", "score": 80,
                 "entry": "$1-$2", "stop": 1.0, "t1": 3.0, "t2": 4.0, "last": 200.0},
        "flow": {"score": 0.7, "confidence": "🟢 Vysoká", "accum_count": 2, "premium": 6_000_000},
        "news": None, "earn_days": None,
    }
    r = ag.genius_fuse(lenses)
    assert "BULLISH" in r["direction"]
    assert r["agree"] is True
    assert r["conflict"] is False
    assert r["score"] >= 70
    assert any("Technika" in p for p in r["pro"])


def test_genius_fuse_conflict_penalizes():
    # technika bullish, flow bearish → rozpor sráží přesvědčení a hlásí riziko
    lenses = {
        "ticker": "TSLA", "last": 250.0,
        "tech": {"setup_type": "🟢 Pullback Buy", "score": 70, "last": 250.0},
        "flow": {"score": -0.7, "confidence": "🟡 Střední", "accum_count": 0, "premium": 1_200_000},
    }
    r = ag.genius_fuse(lenses)
    assert r["conflict"] is True
    assert r["agree"] is False
    assert any("protiřečí" in x for x in r["risk"])


def test_genius_fuse_earnings_risk_flagged():
    lenses = {
        "ticker": "NVDA", "last": 120.0,
        "tech": {"setup_type": "🚀 ATH Breakout", "score": 75, "last": 120.0},
        "flow": {"score": 0.6, "confidence": "🟢 Vysoká", "accum_count": 1, "premium": 5_000_000},
        "earn_days": 3,
    }
    r = ag.genius_fuse(lenses)
    assert r["earn_days"] == 3
    assert any("Earnings" in x for x in r["risk"])


def test_genius_fuse_no_setup_is_zero_tech_bias():
    # „No Setup" → technika přispívá nulou, takže rozhoduje flow
    lenses = {
        "ticker": "MSFT", "last": 400.0,
        "tech": {"setup_type": "⚠️ No Setup", "score": 0, "last": 400.0},
        "flow": {"score": 0.5, "confidence": "🟡 Střední", "accum_count": 0, "premium": 800_000},
    }
    r = ag.genius_fuse(lenses)
    t = next(f for f in r["factors"] if f["name"] == "Technika")
    assert t["bias"] == 0.0
    assert "BULLISH" in r["direction"]   # směr táhne flow


def test_simulate_trade_target_hit():
    # cena dorazí na target → win, R = zisk/riziko
    sim = ag.simulate_trade(100, 95, 110, [105, 111], [99, 104], [104, 108], 5)
    assert sim["outcome"] == "target"
    assert abs(sim["ret"] - 0.10) < 1e-9
    assert abs(sim["r"] - 2.0) < 1e-9      # 10 % zisk / 5 % riziko
    assert sim["bars"] == 2


def test_simulate_trade_stop_hit():
    sim = ag.simulate_trade(100, 95, 110, [101, 102], [96, 94], [100, 95], 5)
    assert sim["outcome"] == "stop"
    assert abs(sim["ret"] + 0.05) < 1e-9
    assert abs(sim["r"] + 1.0) < 1e-9


def test_simulate_trade_stop_priority_same_bar():
    # když svíčka protne stop i target naráz → konzervativně STOP
    sim = ag.simulate_trade(100, 95, 110, [110], [95], [100], 5)
    assert sim["outcome"] == "stop"


def test_simulate_trade_timeout():
    sim = ag.simulate_trade(100, 90, 120, [101, 102, 103], [99, 98, 97], [100, 101, 102], 3)
    assert sim["outcome"] == "timeout"
    assert abs(sim["ret"] - 0.02) < 1e-9
    assert sim["bars"] == 3


def test_simulate_trade_invalid_inputs():
    assert ag.simulate_trade(100, 100, 110, [1], [1], [1], 5) is None   # stop >= entry
    assert ag.simulate_trade(100, 95, 100, [1], [1], [1], 5) is None    # target <= entry


def test_aggregate_edge_math():
    trades = [
        {"ret": 0.10, "r": 2.0, "outcome": "target", "bars": 2},
        {"ret": -0.05, "r": -1.0, "outcome": "stop", "bars": 1},
        {"ret": 0.02, "r": 0.4, "outcome": "timeout", "bars": 3},
    ]
    a = ag._aggregate_edge(trades)
    assert a["n"] == 3
    assert abs(a["wr"] - 2 / 3) < 1e-9
    assert abs(a["exp_r"] - (1.4 / 3)) < 1e-9
    assert abs(a["pf"] - (0.12 / 0.05)) < 1e-9
    assert a["target_hits"] == 1 and a["stop_hits"] == 1 and a["timeouts"] == 1


def test_aggregate_edge_empty():
    assert ag._aggregate_edge([]) is None


# ── Fundamentální scorecard + investiční profil ──────────────────────────────

def test_letter_grade_bands():
    assert ag._letter_grade(95) == "A+"
    assert ag._letter_grade(80) == "A"
    assert ag._letter_grade(66) == "B"
    assert ag._letter_grade(50) == "C"
    assert ag._letter_grade(36) == "D"
    assert ag._letter_grade(10) == "F"
    assert ag._letter_grade(None) == "—"


def test_band_helpers():
    # vyšší = lepší
    assert ag._band_high(40, [(30, 100), (10, 60)], 0) == 100
    assert ag._band_high(15, [(30, 100), (10, 60)], 0) == 60
    assert ag._band_high(5, [(30, 100), (10, 60)], 0) == 0
    # nižší = lepší
    assert ag._band_low(8, [(10, 100), (20, 60)], 0) == 100
    assert ag._band_low(15, [(10, 100), (20, 60)], 0) == 60
    assert ag._band_low(25, [(10, 100), (20, 60)], 0) == 0


def test_score_growth_strong_vs_weak():
    strong = ag.score_growth(40, 35)   # explozivní růst tržeb i EPS
    assert strong["score"] == 100 and strong["grade"] == "A+"
    weak = ag.score_growth(-12, -15)   # propad
    assert weak["score"] < 35 and weak["grade"] == "F"
    # chybějící data → None skóre, ale nespadne
    assert ag.score_growth(None, None)["score"] is None


def test_score_valuation_cheap_is_higher():
    cheap = ag.score_valuation(10, 1.5, 0.9)
    expensive = ag.score_valuation(60, 25, 6.0)
    assert cheap["score"] > expensive["score"]   # levnější = atraktivnější = vyšší skóre


def test_score_balance_net_cash_beats_leverage():
    healthy = ag.score_balance(0.2, 2.5, cash=50, debt=5)
    levered = ag.score_balance(2.8, 0.8, cash=5, debt=60)
    assert healthy["score"] > levered["score"]


def test_invest_profile_great_company_fair_price():
    r = ag.invest_profile(growth=85, profit=90, balance=88, value=70,
                          cashflow=85, trend=80, upside=20)
    assert "Skvělá firma" in r["verdict"]
    assert r["quality"] >= 68 and r["value"] >= 55
    assert r["overall"] >= 70


def test_invest_profile_quality_but_expensive():
    r = ag.invest_profile(growth=90, profit=95, balance=80, value=35,
                          cashflow=90, trend=70, upside=5)
    assert "draho" in r["verdict"]


def test_invest_profile_value_trap():
    r = ag.invest_profile(growth=30, profit=40, balance=45, value=75,
                          cashflow=35, trend=30, upside=10)
    assert "rozbitá" in r["verdict"]   # laciná, ale slabé fundamenty


def test_invest_profile_empty_is_safe():
    r = ag.invest_profile(None, None, None, None, None, None, None)
    assert r["overall"] is None
    assert "Nedostatek dat" in r["verdict"]


def test_entry_ladder_wide_zone_three_tranches():
    L = ag.build_entry_ladder(zone_bot=1.06, zone_top=1.18, last=1.21,
                              atr=0.10, stop=0.99, t1=1.40, t2=1.75)
    assert L["n"] == 3                                  # široká zóna (>=0.6 ATR)
    assert [t["weight"] for t in L["tranches"]] == [0.25, 0.35, 0.40]   # pyramida
    p = [t["price"] for t in L["tranches"]]
    assert p[0] > p[1] > p[2]                           # ceny klesají k spodku zóny


def test_entry_ladder_narrow_zone_two_tranches():
    L = ag.build_entry_ladder(zone_bot=1.15, zone_top=1.18, last=1.21,
                              atr=0.10, stop=1.05, t1=1.40, t2=1.75)
    assert L["n"] == 2                                  # úzká zóna (<0.6 ATR)
    assert [t["weight"] for t in L["tranches"]] == [0.40, 0.60]


def test_entry_ladder_avg_is_pyramid_weighted():
    # Vážený Ø vstup musí být blíž spodku zóny než prostý aritmetický střed.
    L = ag.build_entry_ladder(zone_bot=1.06, zone_top=1.18, last=1.21,
                              atr=0.10, stop=0.99, t1=1.40, t2=1.75)
    assert L["avg_entry"] < (1.06 + 1.18) / 2.0


def test_entry_ladder_rr_from_avg_beats_top_entry():
    # R:R od Ø vstupu musí být lepší než od vršku zóny (lepší Ø cena = vyšší R:R).
    L = ag.build_entry_ladder(zone_bot=1.06, zone_top=1.18, last=1.21,
                              atr=0.10, stop=0.99, t1=1.40, t2=1.75)
    rr1_from_top = (1.40 - 1.18) / (1.18 - 0.99)
    assert L["rr1"] > rr1_from_top > 0
    assert L["rr2"] > L["rr1"]                          # vzdálenější cíl = vyšší R:R
    assert L["stop_pct"] < 0                            # stop je pod vstupem


def test_fmt_price_adaptive_precision():
    assert ag._fmt_price(0.0625) == "$0.0625"          # penny → 4 desetiny
    assert ag._fmt_price(1.234) == "$1.234"            # < 10 → 3 desetiny
    assert ag._fmt_price(187.4) == "$187.40"           # ≥ 10 → 2 desetiny


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
