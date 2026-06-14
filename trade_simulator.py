import pandas as pd
from dataclasses import dataclass
from typing import Optional


@dataclass
class SetupProposal:
    date: str
    ticker: str

    setup_type: str
    score: int
    sm_score: int
    flow_score: float

    zone_bottom: float
    zone_top: float

    entry_price: float

    stop_loss: float
    target1: float
    target2: float

    rr_zone: float

    distance_ema20_pct: float = 0.0
    distance_ema50_pct: float = 0.0
    trend_strength_pct: float = 0.0
    volatility_20d_pct: float = 0.0
    atr14: float = 0.0
    atr14_pct: float = 0.0


@dataclass
class TradeRecord:
    proposal: SetupProposal

    status: str

    entry_date: Optional[str] = None
    exit_date: Optional[str] = None

    days_in_trade: int = 0

    result_r: float = 0.0
    result_pct: float = 0.0


class TradeSimulator:

    MAX_WAIT_BARS = 20

    def run_simulation(
        self,
        setup: SetupProposal,
        future_data: pd.DataFrame
    ) -> TradeRecord:

        record = TradeRecord(
            proposal=setup,
            status="EXPIRED"
        )

        if future_data.empty:
            return record

        risk_per_share = setup.zone_top - setup.stop_loss

        if risk_per_share <= 0:
            return record

        in_trade = False
        actual_entry = None

        h1_closed = False
        h2_closed = False

        h1_r = 0.0
        h2_r = 0.0

        wait_bars = 0

        for index, row in future_data.iterrows():

            date_str = index.strftime("%Y-%m-%d")

            open_p = float(row["Open"])
            high = float(row["High"])
            low = float(row["Low"])

            # ==================================================
            # WAITING FOR ENTRY
            # ==================================================

            if not in_trade:

                wait_bars += 1

                if wait_bars > self.MAX_WAIT_BARS:
                    record.status = "EXPIRED"
                    return record

                # limit buy na zone_top
                if low <= setup.zone_top:

                    in_trade = True

                    record.entry_date = date_str

                    actual_entry = setup.zone_top

                    # okamžitý fail ve stejné svíčce
                    if low <= setup.stop_loss:

                        exit_price = (
                            open_p
                            if open_p < setup.stop_loss
                            else setup.stop_loss
                        )

                        record.status = "LOSS"
                        record.exit_date = date_str

                        record.result_r = -(
                            (actual_entry - exit_price)
                            / risk_per_share
                        )

                        record.result_pct = (
                            (exit_price / actual_entry) - 1
                        ) * 100

                        return record

                continue

            # ==================================================
            # TRADE MANAGEMENT
            # ==================================================

            # STOP má prioritu
            if low <= setup.stop_loss:

                exit_price = (
                    open_p
                    if open_p < setup.stop_loss
                    else setup.stop_loss
                )

                if not h1_closed:
                    h1_r = -(
                        (actual_entry - exit_price)
                        / risk_per_share
                    )

                if not h2_closed:
                    h2_r = -(
                        (actual_entry - exit_price)
                        / risk_per_share
                    )

                record.exit_date = date_str

                if h1_closed:
                    record.status = "PARTIAL_WIN"
                else:
                    record.status = "LOSS"

                break

            # ==================================================
            # TARGET 1
            # ==================================================

            if high >= setup.target1 and not h1_closed:

                h1_closed = True

                h1_r = (
                    setup.target1 - actual_entry
                ) / risk_per_share

            # ==================================================
            # TARGET 2
            # ==================================================

            if high >= setup.target2 and not h2_closed:

                h2_closed = True

                h2_r = (
                    setup.target2 - actual_entry
                ) / risk_per_share

                record.status = "WIN"
                record.exit_date = date_str

                break

        # ==================================================
        # FINAL RESULT
        # ==================================================

        if in_trade:

            if record.exit_date:

                record.result_r = (
                    0.5 * h1_r
                    + 0.5 * h2_r
                )

            else:

                record.status = "OPEN"

                last_close = float(
                    future_data.iloc[-1]["Close"]
                )

                if h1_closed and not h2_closed:

                    open_r = (
                        last_close - actual_entry
                    ) / risk_per_share

                    record.result_r = (
                        0.5 * h1_r
                        + 0.5 * open_r
                    )

                elif not h1_closed:

                    record.result_r = (
                        last_close - actual_entry
                    ) / risk_per_share

                else:

                    record.result_r = (
                        0.5 * h1_r
                        + 0.5 * h2_r
                    )

            record.result_pct = (
                record.result_r
                * risk_per_share
                / actual_entry
            ) * 100

        if record.entry_date and record.exit_date:

            entry_d = pd.to_datetime(
                record.entry_date
            )

            exit_d = pd.to_datetime(
                record.exit_date
            )

            record.days_in_trade = (
                exit_d - entry_d
            ).days

        return record
