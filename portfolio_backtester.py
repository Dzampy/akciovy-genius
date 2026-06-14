from pathlib import Path

import pandas as pd

from backtester import Backtester


class PortfolioBacktester:
    DEFAULT_TICKERS = [
        "AAPL",
        "MSFT",
        "META",
        "AMZN",
        "GOOGL",
        "TSLA",
        "AMD",
        "NFLX",
        "NVDA",
    ]

    def __init__(
        self,
        tickers=None,
        period="10y",
        initial_equity=100000.0,
        risk_per_trade=0.01,
        setup_filters=None,
        output_dir=None,
    ):
        self.tickers = tickers or self.DEFAULT_TICKERS
        self.period = period
        self.initial_equity = float(initial_equity)
        self.risk_per_trade = float(risk_per_trade)
        self.setup_filters = setup_filters
        self.output_dir = Path(output_dir or Path(__file__).resolve().parent)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.backtester = Backtester(setup_filters=setup_filters)

    def run(self):
        all_results = []
        failed = []

        for ticker in self.tickers:
            try:
                result = self.backtester.run(ticker=ticker, period=self.period)
            except Exception as exc:
                print(f"Portfolio error for {ticker}: {exc}")
                failed.append({"ticker": ticker, "error": str(exc)})
                continue

            if result.empty:
                failed.append({"ticker": ticker, "error": "No setups found"})
                continue

            all_results.append(result)

        if not all_results:
            raise ValueError("No portfolio results were generated")

        trades = pd.concat(all_results, ignore_index=True)
        trades = self._prepare_trades(trades)

        equity_curve = self._build_equity_curve(trades)
        summary = self._build_summary(trades, equity_curve, failed)

        trades_path = self.output_dir / "portfolio_trades.csv"
        summary_path = self.output_dir / "portfolio_summary.csv"
        equity_path = self.output_dir / "portfolio_equity_curve.csv"
        equity_svg_path = self.output_dir / "portfolio_equity_curve.svg"

        trades.to_csv(trades_path, index=False)
        summary.to_csv(summary_path, index=False)
        equity_curve.to_csv(equity_path, index=False)
        self._export_equity_curve_svg(equity_curve, equity_svg_path)

        print("\nPORTFOLIO SUMMARY:")
        for _, row in summary.iterrows():
            print(f"{row['metric']}: {row['value']}")

        print(f"\nTrades CSV: {trades_path}")
        print(f"Summary CSV: {summary_path}")
        print(f"Equity curve CSV: {equity_path}")
        print(f"Equity curve SVG: {equity_svg_path}")

        return trades, summary, equity_curve

    def _prepare_trades(self, trades):
        trades = trades.copy()

        for column in ["date", "entry_date", "exit_date"]:
            trades[column] = pd.to_datetime(trades[column], errors="coerce")

        trades["is_executed"] = trades["entry_date"].notna()
        trades["is_win"] = trades["status"].isin(["WIN", "PARTIAL_WIN"])
        trades["is_loss"] = trades["status"].eq("LOSS")

        trades["event_date"] = (
            trades["exit_date"]
            .fillna(trades["entry_date"])
            .fillna(trades["date"])
        )

        return trades.sort_values(
            ["event_date", "ticker", "date"]
        ).reset_index(drop=True)

    def _build_equity_curve(self, trades):
        executed = trades[trades["is_executed"]].copy()

        columns = [
            "date",
            "ticker",
            "status",
            "result_r",
            "trade_return_pct",
            "equity",
            "drawdown_pct",
        ]

        if executed.empty:
            return pd.DataFrame(columns=columns)

        executed = executed.sort_values(
            ["event_date", "ticker", "date"]
        ).reset_index(drop=True)

        equity = self.initial_equity
        peak = equity
        records = []

        for _, trade in executed.iterrows():
            trade_return = float(trade["result_r"]) * self.risk_per_trade
            equity *= 1 + trade_return
            peak = max(peak, equity)
            drawdown = (equity / peak) - 1 if peak else 0.0

            records.append({
                "date": trade["event_date"].date().isoformat(),
                "ticker": trade["ticker"],
                "status": trade["status"],
                "result_r": trade["result_r"],
                "trade_return_pct": trade_return * 100,
                "equity": equity,
                "drawdown_pct": drawdown * 100,
            })

        return pd.DataFrame(records, columns=columns)

    def _build_summary(self, trades, equity_curve, failed):
        total_setups = len(trades)
        executed = trades[trades["is_executed"]]
        wins = executed[executed["is_win"]]
        losses = executed[executed["is_loss"]]
        expired = trades[trades["status"].eq("EXPIRED")]

        positive_r = executed.loc[executed["result_r"] > 0, "result_r"].sum()
        negative_r = executed.loc[executed["result_r"] < 0, "result_r"].sum()

        total_trades = len(executed)
        win_rate = len(wins) / total_trades * 100 if total_trades else 0.0
        total_r = executed["result_r"].sum()
        average_r = executed["result_r"].mean() if total_trades else 0.0
        profit_factor = (
            positive_r / abs(negative_r)
            if negative_r < 0
            else float("inf")
        )

        max_drawdown = (
            equity_curve["drawdown_pct"].min()
            if not equity_curve.empty
            else 0.0
        )

        cagr = self._calculate_cagr(equity_curve)
        robustness = self._assess_robustness(executed)

        rows = [
            ("symbols_tested", len(self.tickers)),
            ("symbols_failed", len(failed)),
            ("total_setups", total_setups),
            ("total_trades", total_trades),
            ("expired_setups", len(expired)),
            ("win_trades", len(wins)),
            ("loss_trades", len(losses)),
            ("win_rate_pct", round(win_rate, 2)),
            ("total_r", round(total_r, 2)),
            ("average_r", round(average_r, 2)),
            ("profit_factor", round(profit_factor, 2)),
            ("max_drawdown_pct", round(max_drawdown, 2)),
            ("cagr_pct", round(cagr, 2)),
            ("initial_equity", round(self.initial_equity, 2)),
            ("final_equity", self._final_equity(equity_curve)),
            ("risk_per_trade_pct", round(self.risk_per_trade * 100, 2)),
            ("robustness", robustness),
        ]

        symbol_rows = self._symbol_summary_rows(executed)
        failed_rows = [
            (f"failed_{item['ticker']}", item["error"])
            for item in failed
        ]

        return pd.DataFrame(
            rows + symbol_rows + failed_rows,
            columns=["metric", "value"]
        )

    def _symbol_summary_rows(self, executed):
        rows = []

        if executed.empty:
            return rows

        grouped = executed.groupby("ticker")

        profitable_symbols = 0

        for ticker, group in grouped:
            wins = group["is_win"].sum()
            total = len(group)
            total_r = group["result_r"].sum()
            average_r = group["result_r"].mean()
            win_rate = wins / total * 100 if total else 0.0

            if total_r > 0:
                profitable_symbols += 1

            rows.extend([
                (f"{ticker}_trades", total),
                (f"{ticker}_win_rate_pct", round(win_rate, 2)),
                (f"{ticker}_total_r", round(total_r, 2)),
                (f"{ticker}_average_r", round(average_r, 2)),
            ])

        rows.append(("profitable_symbols", profitable_symbols))
        return rows

    def _assess_robustness(self, executed):
        if executed.empty:
            return "No executed trades; robustness cannot be assessed."

        by_symbol = executed.groupby("ticker")["result_r"].agg(["count", "sum"])
        profitable = by_symbol[by_symbol["sum"] > 0]
        enough_trades = by_symbol[by_symbol["count"] >= 3]

        if len(profitable) >= 6 and len(enough_trades) >= 6:
            return "Broadly robust across the tested symbols."

        if "NVDA" in profitable.index and len(profitable) <= 3:
            return "Likely concentrated; the edge is not broadly proven beyond NVDA."

        return "Mixed; profitable on several symbols but not clearly broad yet."

    def _calculate_cagr(self, equity_curve):
        if equity_curve.empty:
            return 0.0

        start = pd.to_datetime(equity_curve["date"].iloc[0])
        end = pd.to_datetime(equity_curve["date"].iloc[-1])
        years = (end - start).days / 365.25

        if years <= 0:
            return 0.0

        final_equity = float(equity_curve["equity"].iloc[-1])
        cagr = (final_equity / self.initial_equity) ** (1 / years) - 1
        return cagr * 100

    def _final_equity(self, equity_curve):
        if equity_curve.empty:
            return round(self.initial_equity, 2)

        return round(float(equity_curve["equity"].iloc[-1]), 2)

    def _export_equity_curve_svg(self, equity_curve, output_path):
        width = 1100
        height = 620
        padding = 70

        if equity_curve.empty:
            svg = (
                f'<svg xmlns="http://www.w3.org/2000/svg" '
                f'width="{width}" height="{height}">'
                '<text x="70" y="100" font-family="Arial" '
                'font-size="24">No equity curve data</text></svg>'
            )
            output_path.write_text(svg, encoding="utf-8")
            return

        values = equity_curve["equity"].astype(float).tolist()
        min_equity = min(values)
        max_equity = max(values)
        span = max(max_equity - min_equity, 1)

        points = []
        for index, equity in enumerate(values):
            x = padding
            if len(values) > 1:
                x += index * (width - 2 * padding) / (len(values) - 1)

            y = (
                height
                - padding
                - ((equity - min_equity) / span) * (height - 2 * padding)
            )
            points.append(f"{x:.2f},{y:.2f}")

        start_date = equity_curve["date"].iloc[0]
        end_date = equity_curve["date"].iloc[-1]
        final_equity = values[-1]
        max_drawdown = equity_curve["drawdown_pct"].min()

        svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="100%" height="100%" fill="#ffffff"/>
  <text x="{padding}" y="38" font-family="Arial" font-size="24" fill="#111111">Portfolio Equity Curve</text>
  <text x="{padding}" y="62" font-family="Arial" font-size="13" fill="#555555">{start_date} to {end_date} | Final equity: {final_equity:,.2f} | Max DD: {max_drawdown:.2f}%</text>
  <line x1="{padding}" y1="{height - padding}" x2="{width - padding}" y2="{height - padding}" stroke="#888888" stroke-width="1"/>
  <line x1="{padding}" y1="{padding}" x2="{padding}" y2="{height - padding}" stroke="#888888" stroke-width="1"/>
  <polyline points="{' '.join(points)}" fill="none" stroke="#1266f1" stroke-width="3"/>
  <text x="{padding}" y="{height - 30}" font-family="Arial" font-size="12" fill="#555555">{start_date}</text>
  <text x="{width - padding - 75}" y="{height - 30}" font-family="Arial" font-size="12" fill="#555555">{end_date}</text>
  <text x="12" y="{padding + 5}" font-family="Arial" font-size="12" fill="#555555">{max_equity:,.0f}</text>
  <text x="12" y="{height - padding + 5}" font-family="Arial" font-size="12" fill="#555555">{min_equity:,.0f}</text>
</svg>
'''
        output_path.write_text(svg, encoding="utf-8")
