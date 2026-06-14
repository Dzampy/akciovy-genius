from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf
import plotly.graph_objects as go
from plotly.subplots import make_subplots


@dataclass
class TimeframeLevels:
    timeframe: str
    nearest_support: Optional[float]
    nearest_resistance: Optional[float]
    support_zone: Optional[tuple[float, float]]
    resistance_zone: Optional[tuple[float, float]]
    swing_highs: list[float]
    swing_lows: list[float]
    clustered_levels: list[dict]


def analyze_mtf_levels(ticker: str) -> dict[str, TimeframeLevels]:
    return {
        "4h": analyze_timeframe_levels(ticker, "4h"),
        "1h": analyze_timeframe_levels(ticker, "1h"),
    }


def analyze_timeframe_levels(ticker: str, timeframe: str) -> TimeframeLevels:
    df = _download_ohlcv(ticker, timeframe)

    if df.empty or len(df) < 60:
        return _empty_levels(timeframe)

    last = float(df["Close"].iloc[-1])
    highs, lows = _find_swing_points(df, window=_pivot_window(timeframe))
    tolerance = _level_tolerance(df)

    high_clusters = _cluster_levels(highs, tolerance)
    low_clusters = _cluster_levels(lows, tolerance)
    clusters = high_clusters + low_clusters

    support_clusters = sorted(
        [cluster for cluster in low_clusters if cluster["level"] < last],
        key=lambda cluster: cluster["level"],
        reverse=True,
    )
    resistance_clusters = sorted(
        [cluster for cluster in high_clusters if cluster["level"] > last],
        key=lambda cluster: cluster["level"],
    )

    nearest_support = (
        support_clusters[0]["level"]
        if support_clusters
        else None
    )
    nearest_resistance = (
        resistance_clusters[0]["level"]
        if resistance_clusters
        else None
    )

    return TimeframeLevels(
        timeframe=timeframe,
        nearest_support=nearest_support,
        nearest_resistance=nearest_resistance,
        support_zone=_zone_from_cluster(support_clusters[0]) if support_clusters else None,
        resistance_zone=_zone_from_cluster(resistance_clusters[0]) if resistance_clusters else None,
        swing_highs=[point[1] for point in highs[-8:]],
        swing_lows=[point[1] for point in lows[-8:]],
        clustered_levels=sorted(clusters, key=lambda cluster: cluster["level"]),
    )


def format_level(level: Optional[float]) -> str:
    if level is None:
        return "N/A"

    return f"${level:.2f}"


def format_zone(zone: Optional[tuple[float, float]]) -> str:
    if zone is None:
        return "N/A"

    return f"${zone[0]:.2f} - ${zone[1]:.2f}"


def make_mtf_chart(ticker: str, timeframe: str = "4h"):
    """Return PNG bytes and a short text summary for the given timeframe.

    The chart shows candlesticks, volume and overlays the nearest support/resistance
    zones detected by the analyzer.
    """
    df = _download_ohlcv(ticker, timeframe)
    if df.empty or len(df) < 10:
        return None, f"❌ Nedostatek dat pro {ticker.upper()} {timeframe}."

    levels = analyze_timeframe_levels(ticker, timeframe)

    fmt = "%Y-%m-%d %H:%M"
    x_dates = df.index.strftime(fmt)

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.8, 0.2])

    fig.add_trace(
        go.Candlestick(x=x_dates, open=df["Open"], high=df["High"], low=df["Low"], close=df["Close"],
                       increasing_line_color="#26a69a", decreasing_line_color="#ef5350", name="Cena"),
        row=1, col=1,
    )

    vol_colors = ['#26a69a' if row['Close'] >= row['Open'] else '#ef5350' for _, row in df.iterrows()]
    fig.add_trace(go.Bar(x=x_dates, y=df["Volume"], marker_color=vol_colors, name="Volume"), row=2, col=1)

    # draw support/resistance zones
    try:
        if levels.support_zone is not None:
            fig.add_hrect(y0=levels.support_zone[0], y1=levels.support_zone[1], line_width=0,
                          fillcolor="rgba(38, 166, 154, 0.2)", annotation_text="Support Zone", annotation_position="top left", row=1, col=1)

        if levels.resistance_zone is not None:
            fig.add_hrect(y0=levels.resistance_zone[0], y1=levels.resistance_zone[1], line_width=0,
                          fillcolor="rgba(255, 193, 7, 0.12)", annotation_text="Resistance Zone", annotation_position="top left", row=1, col=1)

        if levels.nearest_support is not None:
            fig.add_hline(y=levels.nearest_support, line_color="#26a69a", line_dash="dash", annotation_text="Nearest S", row=1, col=1)

        if levels.nearest_resistance is not None:
            fig.add_hline(y=levels.nearest_resistance, line_color="#ef5350", line_dash="dash", annotation_text="Nearest R", row=1, col=1)
    except Exception:
        pass

    fig.update_layout(title=f"MTF S/R: {ticker.upper()} {timeframe}", template="plotly_dark", width=1100, height=700, showlegend=False,
                      margin=dict(l=40, r=40, t=60, b=20))
    fig.update_xaxes(rangeslider_visible=False, type="category", nticks=8)

    png = fig.to_image(format="png")

    summary = (
        f"S/R {ticker.upper()} {timeframe}: support={format_level(levels.nearest_support)} zone={format_zone(levels.support_zone)} | "
        f"resistance={format_level(levels.nearest_resistance)} zone={format_zone(levels.resistance_zone)}"
    )

    return png, summary


def _download_ohlcv(ticker: str, timeframe: str) -> pd.DataFrame:
    interval = "1h"
    period = "1y" if timeframe == "4h" else "3mo"

    df = yf.download(
        ticker,
        period=period,
        interval=interval,
        auto_adjust=True,
        progress=False,
        group_by="ticker",
    )

    if df.empty:
        return df

    df = _normalize_columns(df, ticker)

    if timeframe == "4h":
        df = df.resample("4h").agg({
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }).dropna()

    return df.dropna()


def _normalize_columns(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if not isinstance(df.columns, pd.MultiIndex):
        return df

    ticker = ticker.upper()

    if ticker in df.columns.get_level_values(0):
        return df[ticker]

    if ticker in df.columns.get_level_values(-1):
        return df.xs(ticker, axis=1, level=-1)

    df = df.copy()
    df.columns = df.columns.get_level_values(-1)
    return df


def _find_swing_points(df: pd.DataFrame, window: int) -> tuple[list[tuple[pd.Timestamp, float]], list[tuple[pd.Timestamp, float]]]:
    highs = []
    lows = []

    for index in range(window, len(df) - window):
        segment = df.iloc[index - window:index + window + 1]
        high = float(df["High"].iloc[index])
        low = float(df["Low"].iloc[index])

        if high == float(segment["High"].max()):
            highs.append((df.index[index], high))

        if low == float(segment["Low"].min()):
            lows.append((df.index[index], low))

    return highs, lows


def _cluster_levels(points: list[tuple[pd.Timestamp, float]], tolerance: float) -> list[dict]:
    if not points:
        return []

    points = sorted(points, key=lambda point: point[1])
    clusters = []
    current = [points[0]]

    for point in points[1:]:
        average = float(np.mean([item[1] for item in current]))
        if abs(point[1] - average) <= tolerance:
            current.append(point)
        else:
            clusters.append(_cluster_to_dict(current))
            current = [point]

    clusters.append(_cluster_to_dict(current))
    return clusters


def _cluster_to_dict(cluster: list[tuple[pd.Timestamp, float]]) -> dict:
    prices = [point[1] for point in cluster]

    return {
        "level": float(np.mean(prices)),
        "zone_low": float(min(prices)),
        "zone_high": float(max(prices)),
        "touches": len(cluster),
        "last_touch": max(point[0] for point in cluster).isoformat(),
    }


def _zone_from_cluster(cluster: dict) -> tuple[float, float]:
    return cluster["zone_low"], cluster["zone_high"]


def _level_tolerance(df: pd.DataFrame) -> float:
    high_low_range = (df["High"] - df["Low"]).rolling(20).mean().iloc[-1]

    if pd.isna(high_low_range) or high_low_range <= 0:
        close = float(df["Close"].iloc[-1])
        return close * 0.005

    return float(high_low_range) * 0.5


def _pivot_window(timeframe: str) -> int:
    if timeframe == "4h":
        return 5

    return 8


def _empty_levels(timeframe: str) -> TimeframeLevels:
    return TimeframeLevels(
        timeframe=timeframe,
        nearest_support=None,
        nearest_resistance=None,
        support_zone=None,
        resistance_zone=None,
        swing_highs=[],
        swing_lows=[],
        clustered_levels=[],
    )
