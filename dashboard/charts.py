"""Plotly figures for the dashboard."""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from tradebot import indicators as ind

UP, DOWN = "#16a34a", "#dc2626"
BUY_C, SELL_C = "#16a34a", "#dc2626"
LINE_COLORS = ["#2563eb", "#f59e0b", "#7c3aed", "#0891b2", "#db2777", "#65a30d", "#ea580c", "#475569"]


def _axis_breaks(fig: go.Figure, market: str) -> None:
    if market != "crypto":  # stocks don't trade on weekends; crypto does
        fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])


def price_chart(
    df: pd.DataFrame,
    signals: pd.DataFrame,
    market: str,
    display_from: Optional[pd.Timestamp] = None,
    levels: Optional[Dict[str, float]] = None,
) -> go.Figure:
    """Candles + SMAs + Bollinger + BUY/SELL markers, with volume, RSI and MACD panels.

    Indicators are computed on the full history, then trimmed to `display_from`, so
    the long averages are valid from the first visible bar.
    """
    close = df["close"]
    overlay = pd.DataFrame({
        "SMA 20": ind.sma(close, 20), "SMA 50": ind.sma(close, 50), "SMA 200": ind.sma(close, 200),
    })
    bb = ind.bollinger(close)
    r = ind.rsi(close)
    m = ind.macd(close)

    if display_from is not None:
        keep = df.index >= display_from
        df, signals, overlay, bb, r, m = (x[keep] for x in (df, signals, overlay, bb, r, m))

    fig = make_subplots(rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.025,
                        row_heights=[0.56, 0.12, 0.14, 0.18],
                        subplot_titles=("", "Volume", "RSI (14)", "MACD (12, 26, 9)"))

    fig.add_trace(go.Scatter(x=bb.index, y=bb["upper"], line=dict(width=0), hoverinfo="skip",
                             showlegend=False), row=1, col=1)
    fig.add_trace(go.Scatter(x=bb.index, y=bb["lower"], line=dict(width=0), fill="tonexty",
                             fillcolor="rgba(100,116,139,0.10)", name="Bollinger (20, 2)",
                             hoverinfo="skip"), row=1, col=1)
    fig.add_trace(go.Candlestick(x=df.index, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
                                 name="Price", increasing_line_color=UP, decreasing_line_color=DOWN,
                                 showlegend=False), row=1, col=1)
    for (name, series), color in zip(overlay.items(), ["#f59e0b", "#2563eb", "#7c3aed"]):
        fig.add_trace(go.Scatter(x=series.index, y=series, name=name, line=dict(width=1.4, color=color)),
                      row=1, col=1)

    buys = signals[signals["signal"] == 1]
    sells = signals[signals["signal"] == -1]
    if not buys.empty:
        fig.add_trace(go.Scatter(
            x=buys.index, y=df.loc[buys.index, "low"] * 0.97, mode="markers", name="BUY signal",
            marker=dict(symbol="triangle-up", size=13, color=BUY_C, line=dict(width=1, color="white")),
            text=buys["reason"], hovertemplate="%{x|%Y-%m-%d}<br>BUY: %{text}<extra></extra>"), row=1, col=1)
    if not sells.empty:
        fig.add_trace(go.Scatter(
            x=sells.index, y=df.loc[sells.index, "high"] * 1.03, mode="markers", name="SELL signal",
            marker=dict(symbol="triangle-down", size=13, color=SELL_C, line=dict(width=1, color="white")),
            text=sells["reason"], hovertemplate="%{x|%Y-%m-%d}<br>SELL: %{text}<extra></extra>"), row=1, col=1)

    for label, price in (levels or {}).items():
        color = DOWN if "stop" in label.lower() else (UP if "target" in label.lower() else "#475569")
        fig.add_hline(y=price, line=dict(color=color, dash="dash", width=1.2), row=1, col=1,
                      annotation_text=f"{label} {price:,.4g}", annotation_position="top left")

    vol_colors = [UP if c >= o else DOWN for o, c in zip(df["open"], df["close"])]
    fig.add_trace(go.Bar(x=df.index, y=df["volume"], marker_color=vol_colors, opacity=0.6,
                         name="Volume", showlegend=False), row=2, col=1)

    fig.add_trace(go.Scatter(x=r.index, y=r, name="RSI", line=dict(color="#7c3aed", width=1.4),
                             showlegend=False), row=3, col=1)
    for lvl in (30, 70):
        fig.add_hline(y=lvl, line=dict(color="#94a3b8", dash="dot", width=1), row=3, col=1)

    hist_colors = [UP if v >= 0 else DOWN for v in m["hist"].fillna(0)]
    fig.add_trace(go.Bar(x=m.index, y=m["hist"], marker_color=hist_colors, opacity=0.5,
                         name="MACD hist", showlegend=False), row=4, col=1)
    fig.add_trace(go.Scatter(x=m.index, y=m["macd"], name="MACD", line=dict(color="#2563eb", width=1.3),
                             showlegend=False), row=4, col=1)
    fig.add_trace(go.Scatter(x=m.index, y=m["signal"], name="Signal", line=dict(color="#f59e0b", width=1.3),
                             showlegend=False), row=4, col=1)

    fig.update_layout(height=860, margin=dict(l=10, r=10, t=30, b=10), hovermode="x unified",
                      xaxis_rangeslider_visible=False,
                      legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0))
    fig.update_yaxes(range=[0, 100], row=3, col=1)
    _axis_breaks(fig, market)
    return fig


def line_chart(curves: Dict[str, pd.Series], title: str = "", y_title: str = "",
               percent: bool = False, height: int = 380) -> go.Figure:
    fig = go.Figure()
    for (name, s), color in zip(curves.items(), LINE_COLORS * 3):
        dash = "dot" if name.lower().startswith(("buy", "benchmark")) else "solid"
        fig.add_trace(go.Scatter(x=s.index, y=s, name=name, line=dict(color=color, width=1.8, dash=dash)))
    fig.update_layout(title=title, height=height, margin=dict(l=10, r=10, t=40 if title else 10, b=10),
                      hovermode="x unified", legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0),
                      yaxis_title=y_title)
    if percent:
        fig.update_yaxes(tickformat=".0%")
    return fig


def drawdown_chart(equity: pd.Series, height: int = 220) -> go.Figure:
    dd = equity / equity.cummax() - 1
    fig = go.Figure(go.Scatter(x=dd.index, y=dd, fill="tozeroy", line=dict(color=DOWN, width=1),
                               fillcolor="rgba(220,38,38,0.18)", name="Drawdown"))
    fig.update_layout(height=height, margin=dict(l=10, r=10, t=30, b=10), title="Drawdown",
                      yaxis_tickformat=".0%", showlegend=False)
    return fig
