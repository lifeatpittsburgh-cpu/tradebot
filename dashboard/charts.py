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


def fmt_price(x: float) -> str:
    """Readable price: 60,812.34 / 187.40 / 0.4521 / 0.00001234 (never scientific notation)."""
    import math

    x = float(x)
    if not math.isfinite(x) or x == 0:
        return f"{x:,.2f}"
    decimals = 2 if abs(x) >= 1 else min(10, 3 - int(math.floor(math.log10(abs(x)))))
    return f"{x:,.{decimals}f}"


# Regular trading hours (local time, as decimal hours) used to hide overnight gaps on intraday charts.
_SESSION_HOURS = {"us": (9.5, 16), "india": (9.25, 15.5)}


def _axis_breaks(fig: go.Figure, market: str, intraday: bool = False) -> None:
    if market == "crypto":  # trades 24/7: no gaps to hide
        return
    breaks = [dict(bounds=["sat", "mon"])]  # stocks don't trade on weekends
    if intraday and market in _SESSION_HOURS:
        open_h, close_h = _SESSION_HOURS[market]
        breaks.append(dict(bounds=[close_h, open_h], pattern="hour"))
    fig.update_xaxes(rangebreaks=breaks)


def price_chart(
    df: pd.DataFrame,
    signals: pd.DataFrame,
    market: str,
    display_from: Optional[pd.Timestamp] = None,
    levels: Optional[Dict[str, float]] = None,
    intraday: bool = False,
    initial_bars: Optional[int] = None,
) -> go.Figure:
    """Candles + SMAs + Bollinger + BUY/SELL markers, with volume, RSI and MACD panels.

    Indicators are computed on the full history, then trimmed to `display_from`, so
    the long averages are valid from the first visible bar. Pass `intraday=True` for
    minute/hour bars so overnight gaps are hidden and hover shows the time.
    `initial_bars` opens the chart on only the latest N bars (pan or zoom out for the rest),
    so candles stay readable when the period holds hundreds of them.
    """
    close = df["close"]
    overlay = pd.DataFrame({
        "SMA 20": ind.sma(close, 20), "SMA 50": ind.sma(close, 50), "SMA 200": ind.sma(close, 200),
    })
    bb = ind.bollinger(close)
    r = ind.rsi(close)
    m = ind.macd(close)
    # Markers sit one ATR beyond the candle: close on minute bars, clear of the wicks on daily ones.
    gap = ind.atr(df).bfill().fillna((df["high"] - df["low"]).mean())

    if display_from is not None:
        keep = df.index >= display_from
        df, signals, overlay, bb, r, m, gap = (x[keep] for x in (df, signals, overlay, bb, r, m, gap))

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

    xfmt = "%Y-%m-%d %H:%M" if intraday else "%Y-%m-%d"
    buys = signals[signals["signal"] == 1]
    sells = signals[signals["signal"] == -1]
    if not buys.empty:
        fig.add_trace(go.Scatter(
            x=buys.index, y=df.loc[buys.index, "low"] - gap[buys.index], mode="markers", name="BUY signal",
            marker=dict(symbol="triangle-up", size=11, color=BUY_C, line=dict(width=1, color="white")),
            text=buys["reason"], hovertemplate=f"%{{x|{xfmt}}}<br>BUY: %{{text}}<extra></extra>"), row=1, col=1)
    if not sells.empty:
        fig.add_trace(go.Scatter(
            x=sells.index, y=df.loc[sells.index, "high"] + gap[sells.index], mode="markers", name="SELL signal",
            marker=dict(symbol="triangle-down", size=11, color=SELL_C, line=dict(width=1, color="white")),
            text=sells["reason"], hovertemplate=f"%{{x|{xfmt}}}<br>SELL: %{{text}}<extra></extra>"), row=1, col=1)

    for label, price in (levels or {}).items():
        color = DOWN if "stop" in label.lower() else (UP if "target" in label.lower() else "#475569")
        fig.add_hline(y=price, line=dict(color=color, dash="dash", width=1.2), row=1, col=1,
                      annotation_text=f"{label} {fmt_price(price)}", annotation_position="top left")

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
    if initial_bars and len(df) > initial_bars:
        step = df.index[-1] - df.index[-2]
        fig.update_xaxes(range=[df.index[-initial_bars] - step / 2, df.index[-1] + step * 2])
    _axis_breaks(fig, market, intraday)
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


# Zoom handling for price_chart. Plotly's own zoom scales the price axis along with time, so
# candles spill off the panel. Instead the price axes stay fixed for the user, time zooms (mouse
# wheel, +/- buttons, box zoom from the toolbar), and this script re-fits every panel to the bars in view.
_AUTOFIT_JS = """
<script>
(function () {
  const gd = document.getElementById("{div_id}");
  const FIXED = new Set({fixed_axes});
  const toMs = v => (typeof v === "number" ? v : Date.parse(String(v).replace(" ", "T")));
  let busy = false;
  function fit() {
    if (busy) return;
    const r = gd._fullLayout.xaxis.range;
    const lo = toMs(r[0]), hi = toMs(r[1]);
    const ext = {};
    gd._fullData.forEach(t => {
      if (t.visible === false || !t.x) return;
      const ax = "yaxis" + (t.yaxis === "y" ? "" : t.yaxis.slice(1));
      if (FIXED.has(ax)) return;
      const tops = t.type === "candlestick" ? t.high : t.y;
      const bots = t.type === "candlestick" ? t.low : t.y;
      if (!tops) return;
      const e = ext[ax] || (ext[ax] = [Infinity, -Infinity]);
      if (t.type === "bar") { e[0] = Math.min(e[0], 0); e[1] = Math.max(e[1], 0); }
      for (let i = 0; i < t.x.length; i++) {
        const x = toMs(t.x[i]);
        if (x < lo || x > hi) continue;
        const a = +bots[i], b = +tops[i];
        if (isFinite(a)) e[0] = Math.min(e[0], a);
        if (isFinite(b)) e[1] = Math.max(e[1], b);
      }
    });
    const upd = {};
    for (const [ax, [a, b]] of Object.entries(ext)) {
      if (!isFinite(a) || !isFinite(b)) continue;
      const pad = (b - a || Math.abs(b) || 1) * 0.05;
      upd[ax + ".range"] = [a - pad, b + pad];
    }
    busy = true;
    Plotly.relayout(gd, upd).finally(() => { busy = false; });
  }
  window.__chartReady.then(() => {
  gd.on("plotly_relayout", ev => {
    if (!busy && Object.keys(ev).some(k => k.startsWith("xaxis"))) fit();
  });
  fit();
  });
})();
</script>
"""


def chart_html(fig: go.Figure, dark: bool = False, fixed_axes=("yaxis3",)) -> str:
    """Standalone HTML for `price_chart` with time-only zoom and auto-fitting price panels.

    `fixed_axes` keep their own range while zooming (the RSI panel stays 0-100).
    """
    fig = go.Figure(fig)
    fig.update_yaxes(fixedrange=True)  # zoom acts on time only; the script re-fits prices
    fig.update_layout(margin=dict(t=70), legend=dict(y=1.04),  # room for the toolbar above the legend
                      dragmode="pan", template="plotly_dark" if dark else "plotly_white",
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(family="Arial, Helvetica, sans-serif"),
                      # The unified hover box takes the (transparent) paper colour by default and becomes unreadable.
                      hoverlabel=dict(bgcolor="#1e293b" if dark else "#ffffff",
                                      font=dict(color="#f1f5f9" if dark else "#0f172a"),
                                      bordercolor="#475569" if dark else "#cbd5e1"))  # a named font: plotly mis-measures the bare generic one
    div_id = "price-chart"
    # A full document (with <!DOCTYPE>): in quirks mode Plotly mis-measures legend text and the labels overlap.
    html = fig.to_html(full_html=True, include_plotlyjs="cdn", div_id=div_id,
                       config={"displaylogo": False, "scrollZoom": True, "responsive": True,
                               "modeBarButtonsToRemove": ["select2d", "lasso2d", "autoScale2d"]})
    script = _AUTOFIT_JS.replace("{div_id}", div_id).replace("{fixed_axes}", str(list(fixed_axes)).replace("'", '"'))
    style = "<style>html,body{margin:0;padding:0;background:transparent}</style>"
    # Streamlit renders every tab up front. Plotly drawn inside a hidden tab caches its text sizes
    # as zero (overlapping legend), so hold the first draw until the chart is actually visible.
    defer = ("<script>window.__chartReady=new Promise(function(done){window.__drawWhenVisible=function(){"
             "var a=arguments,el=document.getElementById(a[0]);"
             "function go(){Plotly.newPlot.apply(Plotly,a).then(done);}"
             "if(el.offsetWidth>0){go();return;}"
             "var ro=new ResizeObserver(function(){if(el.offsetWidth>0){ro.disconnect();go();}});ro.observe(el);};});"
             "</script>")
    html = html.replace("Plotly.newPlot(", "window.__drawWhenVisible(", 1)
    return html.replace("<head>", "<head>" + style + defer, 1).replace("</body>", script + "</body>", 1)
