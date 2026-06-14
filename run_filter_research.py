from pathlib import Path

import pandas as pd

from portfolio_backtester import PortfolioBacktester


CONFIGS = [
    {
        "name": "baseline",
        "filters": None,
    },
    {
        "name": "filter_1_low_volatility",
        "filters": {
            "max_volatility_20d_pct": 3.0,
        },
    },
    {
        "name": "filter_2_exclude_tsla",
        "filters": {
            "exclude_tickers": ["TSLA"],
        },
    },
    {
        "name": "combined_filters",
        "filters": {
            "max_volatility_20d_pct": 3.0,
            "exclude_tickers": ["TSLA"],
        },
    },
]


def main():
    project_dir = Path(__file__).resolve().parent
    output_root = project_dir / "filter_research"
    rows = []

    for config in CONFIGS:
        print(f"\n=== Running {config['name']} ===")

        backtester = PortfolioBacktester(
            period="10y",
            initial_equity=100000,
            risk_per_trade=0.01,
            setup_filters=config["filters"],
            output_dir=output_root / config["name"],
        )

        _, summary, _ = backtester.run()
        row = {"strategy": config["name"]}
        row.update(_summary_to_dict(summary))
        rows.append(row)

    comparison = pd.DataFrame(rows)
    comparison_path = project_dir / "filter_comparison.csv"
    comparison.to_csv(comparison_path, index=False)

    print("\nFILTER COMPARISON:")
    print(comparison[[
        "strategy",
        "total_trades",
        "win_rate_pct",
        "total_r",
        "average_r",
        "profit_factor",
        "max_drawdown_pct",
        "cagr_pct",
        "profitable_symbols",
    ]])
    print(f"\nComparison CSV: {comparison_path}")


def _summary_to_dict(summary):
    result = {}

    for _, row in summary.iterrows():
        result[row["metric"]] = row["value"]

    return result


if __name__ == "__main__":
    main()
