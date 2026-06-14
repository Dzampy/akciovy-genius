import pandas as pd
import yfinance as yf
from pathlib import Path

from trade_simulator import TradeSimulator
from setup_engine import generate_setup


print(f"DEBUG backtester.py imported from: {Path(__file__).resolve()}")
print(f"DEBUG generate_setup available in backtester: {callable(generate_setup)}")


class Backtester:

    def __init__(self, setup_filters=None):
        self.simulator = TradeSimulator()
        self.setup_filters = setup_filters

    def run(
        self,
        ticker: str,
        period: str = "10y"
    ) -> pd.DataFrame:

        print(f"\nLoading {ticker}...\n")

        df = yf.download(
            ticker,
            period=period,
            auto_adjust=True,
            progress=False
        )

        df = self._normalize_downloaded_data(df, ticker)

        if len(df) < 300:
            raise ValueError("Not enough history")

        trades = []

        active_until = None

        total_bars = len(df)

        for i in range(250, total_bars - 30):

            current_date = df.index[i]

            if active_until is not None:
                if current_date <= active_until:
                    continue

            history = df.iloc[:i].copy()

            try:
                setup = generate_setup(
                    history,
                    ticker,
                    filters=self.setup_filters,
                )

            except Exception as e:
                print(f"Setup error: {e}")
                continue

            if setup is None:
                continue

            future_data = df.iloc[i + 1:].copy()

            trade = self.simulator.run_simulation(
                setup,
                future_data
            )

            trades.append(trade)

            if trade.entry_date and trade.exit_date:

                active_until = pd.to_datetime(
                    trade.exit_date
                )

            elif trade.entry_date:

                active_until = df.index[-1]

            if len(trades) % 25 == 0:
                print(
                    f"Trades: {len(trades)} | "
                    f"Progress: {i}/{total_bars}"
                )

        print(
            f"\nFinished. "
            f"{len(trades)} trades found.\n"
        )

        records = []

        for t in trades:

            records.append({
                "ticker": t.proposal.ticker,
                "date": t.proposal.date,
                "setup_type": t.proposal.setup_type,
                "score": t.proposal.score,
                "sm_score": t.proposal.sm_score,
                "flow_score": t.proposal.flow_score,
                "status": t.status,
                "entry_date": t.entry_date,
                "exit_date": t.exit_date,
                "days_in_trade": t.days_in_trade,
                "result_r": t.result_r,
                "result_pct": t.result_pct,
                "rr_zone": t.proposal.rr_zone,
                "distance_ema20_pct": t.proposal.distance_ema20_pct,
                "distance_ema50_pct": t.proposal.distance_ema50_pct,
                "trend_strength_pct": t.proposal.trend_strength_pct,
                "volatility_20d_pct": t.proposal.volatility_20d_pct,
                "atr14": t.proposal.atr14,
                "atr14_pct": t.proposal.atr14_pct,
            })

        return pd.DataFrame(records)

    @staticmethod
    def _normalize_downloaded_data(
        df: pd.DataFrame,
        ticker: str
    ) -> pd.DataFrame:
        if df.empty:
            return df

        if isinstance(df.columns, pd.MultiIndex):
            if ticker in df.columns.get_level_values(-1):
                df = df.xs(ticker, axis=1, level=-1)
            elif ticker in df.columns.get_level_values(0):
                df = df.xs(ticker, axis=1, level=0)
            else:
                df.columns = df.columns.get_level_values(0)

        return df.dropna()
