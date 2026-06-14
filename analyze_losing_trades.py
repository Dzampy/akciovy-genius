from pathlib import Path

import pandas as pd


FEATURES = [
    "distance_ema20_pct",
    "distance_ema50_pct",
    "trend_strength_pct",
    "volatility_20d_pct",
    "atr14_pct",
]


def main():
    project_dir = Path(__file__).resolve().parent
    source_path = project_dir / "filter_research" / "baseline" / "portfolio_trades.csv"

    if not source_path.exists():
        source_path = project_dir / "portfolio_trades.csv"

    trades = pd.read_csv(source_path)
    executed = trades[trades["is_executed"].astype(str).str.lower().eq("true")].copy()
    losers = executed[executed["status"].eq("LOSS")].copy()
    winners = executed[executed["status"].isin(["WIN", "PARTIAL_WIN"])].copy()

    losers.to_csv(project_dir / "losing_trades_analysis.csv", index=False)

    comparison = _winner_loser_comparison(winners, losers)
    comparison.to_csv(project_dir / "winner_loser_feature_comparison.csv", index=False)

    ticker_breakdown = _ticker_breakdown(executed)
    ticker_breakdown.to_csv(project_dir / "ticker_failure_breakdown.csv", index=False)

    print("Losing trades:", len(losers))
    print("Winner/loser comparison:")
    print(comparison)
    print("\nTicker breakdown:")
    print(ticker_breakdown)


def _winner_loser_comparison(winners, losers):
    rows = []

    for feature in FEATURES:
        rows.append({
            "feature": feature,
            "winner_mean": winners[feature].mean(),
            "loser_mean": losers[feature].mean(),
            "winner_median": winners[feature].median(),
            "loser_median": losers[feature].median(),
            "winner_p25": winners[feature].quantile(0.25),
            "loser_p25": losers[feature].quantile(0.25),
            "winner_p75": winners[feature].quantile(0.75),
            "loser_p75": losers[feature].quantile(0.75),
        })

    return pd.DataFrame(rows).round(4)


def _ticker_breakdown(executed):
    grouped = executed.groupby("ticker")

    rows = []
    for ticker, group in grouped:
        wins = group["status"].isin(["WIN", "PARTIAL_WIN"]).sum()
        losses = group["status"].eq("LOSS").sum()
        total = len(group)

        rows.append({
            "ticker": ticker,
            "trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": wins / total * 100 if total else 0.0,
            "total_r": group["result_r"].sum(),
            "average_r": group["result_r"].mean(),
            "avg_volatility_20d_pct": group["volatility_20d_pct"].mean(),
            "avg_atr14_pct": group["atr14_pct"].mean(),
        })

    return pd.DataFrame(rows).round(4).sort_values("average_r")


if __name__ == "__main__":
    main()
