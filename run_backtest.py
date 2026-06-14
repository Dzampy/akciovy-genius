from pathlib import Path

from backtester import Backtester


def main():
    print("START")

    ticker = "NVDA"
    project_dir = Path(__file__).resolve().parent
    output_path = project_dir / f"backtest_results_{ticker}.csv"

    bt = Backtester()

    results = bt.run(
        ticker=ticker,
        period="10y"
    )

    results.to_csv(output_path, index=False)

    print(results.head())

    print(f"\nTrades: {len(results)}")
    print("\nSTATUS:")
    print(results["status"].value_counts())

    entered = results[results["entry_date"].notna()]
    wins = entered[entered["status"].isin(["WIN", "PARTIAL_WIN"])]
    expired = results[results["status"] == "EXPIRED"]
    win_rate = (len(wins) / len(entered) * 100) if len(entered) else 0

    print("\nSUMMARY:")
    print(f"Setups total: {len(results)}")
    print(f"Entered trades: {len(entered)}")
    print(f"Expired setups: {len(expired)}")
    print(f"Win rate from entered trades: {win_rate:.2f}%")
    print(f"Total R: {results['result_r'].sum():.2f}")
    print(f"Average R per setup: {results['result_r'].mean():.2f}")
    print(f"Average R per entered trade: {entered['result_r'].mean():.2f}")
    print(f"CSV saved to: {output_path}")


if __name__ == "__main__":
    main()
