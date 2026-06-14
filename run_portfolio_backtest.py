from portfolio_backtester import PortfolioBacktester
from setup_engine import FINAL_STRATEGY_FILTERS


def main():
    backtester = PortfolioBacktester(
        period="10y",
        initial_equity=100000,
        risk_per_trade=0.01,
        setup_filters=FINAL_STRATEGY_FILTERS,
    )
    backtester.run()


if __name__ == "__main__":
    main()
