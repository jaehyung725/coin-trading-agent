"""Indicator calculations and versioned strategy signals shared by the app and agent."""

import pandas as pd

STRATEGIES = {
    "bollinger": "볼린저 밴드",
    "sma20": "SMA 20",
    "ema20": "EMA 20",
    "rsi14": "RSI 14",
    "macd": "MACD",
}
STRATEGY_VERSION = 1
LEVERAGE_LEVELS = {"Light": 2, "Medium": 5, "High": 10, "Ultra": 20}
ENTRY_PERCENTAGES = (20, 40, 60, 80, 100)
# Use a fixed warm-up for recursive indicators so live and replay use identical inputs.
WARMUP = {"sma20": 21, "ema20": 100, "rsi14": 100, "macd": 100}

def indicator_series(candles, indicators, settings):
    closes = pd.Series([candle.closing for candle in candles], dtype=float)
    series = {}
    if "볼린저 밴드" in indicators:
        window = closes.rolling(settings["period"])
        center = window.mean()
        spread = window.std(ddof=0) * settings["deviations"]
        series.update({"밴드 상단": center + spread, "밴드 중심선": center, "밴드 하단": center - spread})
    if "SMA 20" in indicators:
        series["SMA 20"] = closes.rolling(20).mean()
    if "EMA 20" in indicators:
        series["EMA 20"] = closes.ewm(span=20, min_periods=20, adjust=False).mean()
    if "RSI 14" in indicators:
        changes = closes.diff().dropna()
        if len(changes) >= 14:
            gains, losses = changes.clip(lower=0), -changes.clip(upper=0)
            avg_gain = pd.concat([pd.Series({14: gains.iloc[:14].mean()}), gains.iloc[14:]]).ewm(
                alpha=1 / 14, adjust=False
            ).mean()
            avg_loss = pd.concat([pd.Series({14: losses.iloc[:14].mean()}), losses.iloc[14:]]).ewm(
                alpha=1 / 14, adjust=False
            ).mean()
            series["RSI 14"] = (100 - 100 / (1 + avg_gain / avg_loss)).mask(
                avg_gain.eq(0) & avg_loss.eq(0), 50
            ).reindex(closes.index)
    if "MACD" in indicators:
        fast = closes.ewm(span=12, min_periods=12, adjust=False).mean()
        slow = closes.ewm(span=26, min_periods=26, adjust=False).mean()
        macd = fast - slow
        signal = macd.ewm(span=9, min_periods=9, adjust=False).mean()
        series.update({"MACD": macd, "시그널": signal, "히스토그램": macd - signal})
    return series


def indicator_signal(history, strategy, holding):
    label = STRATEGIES[strategy]
    series = indicator_series(history, (label,), {})
    if strategy == "rsi14":
        previous, current = series[label].iloc[-2:]
        buy = previous < 30 <= current
        sell = previous > 70 >= current
        return ("SELL" if sell else None) if holding else ("BUY" if buy else None)
    if strategy == "macd":
        difference = series["히스토그램"]
    else:
        difference = pd.Series([candle.closing for candle in history]) - series[label]
    previous, current = difference.iloc[-2:]
    if holding:
        return "SELL" if previous >= 0 > current else None
    return "BUY" if previous <= 0 < current else None
