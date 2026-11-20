"""Local dashboard for the paper trading agent."""

import csv
import io
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from html import escape
from tempfile import mkstemp
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError
from urllib.request import urlopen

import altair as alt
import pandas as pd
import streamlit as st

from leveraged_paper import Long, merge_legacy_cash, paper_long
from amount_input import amount_input
from market_grid import render_market_grid
from theme_picker import render_theme_picker
from price_colors import NEGATIVE, NEUTRAL, POSITIVE, direction_color
from supabase_accounts import (SupabaseError, account_changes, account_directory, current_login,
                               load_config, login_dialog, navigate, show_account_controls,
                               show_account_settings, show_auth_page, show_storage_error)

from agent import (Account, adjust_portfolio_cash,
                   initialize_account, manual_fill, paper_limit_orders, recent_candles)

KST = timezone(timedelta(hours=9))
SIDE_LABELS = {"BUY": "매수", "SELL": "매도"}
INDICATORS = ("볼린저 밴드", "SMA 20", "EMA 20", "RSI 14", "MACD")


def local_time(timestamp):
    return datetime.fromisoformat(timestamp).astimezone(KST)


def read_account(path):
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        account = db.execute("SELECT * FROM account WHERE id = 1").fetchone()
        fills = db.execute("SELECT * FROM fills ORDER BY time").fetchall()
        has_adjustments = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='cash_adjustments' AND type='table'"
        ).fetchone()
        adjustments = db.execute("SELECT * FROM cash_adjustments ORDER BY time").fetchall() if has_adjustments else []
    return account, fills, adjustments


def trade_rows(fills, fee_rate):
    rows = []
    previous_units = 0.0
    entry_cost = 0.0
    for fill in fills:
        units = fill["units"]
        quantity = abs(units - previous_units)
        fee = quantity * fill["price"] * fee_rate
        gross = quantity * fill["price"]
        profit = None
        if fill["side"] == "BUY":
            entry_cost += gross + fee
        else:
            sold_cost = entry_cost * (quantity / previous_units)
            profit = gross - fee - sold_cost
            entry_cost -= sold_cost
        rows.append({
            "체결 시각 (KST)": local_time(fill["time"]).strftime("%Y-%m-%d %H:%M:%S"),
            "구분": SIDE_LABELS[fill["side"]],
            "체결가": fill["price"],
            "수량": quantity,
            "수수료": round(fee, 2),
            "실현 손익": profit,
            "체결 후 평균 매입가": entry_cost / units if units else None,
            "체결 후 현금": fill["cash"],
        })
        previous_units = units
    return rows[::-1]


LONG_TRADE_LABELS = ("롱 진입", "롱 종료", "강제청산")


def spot_execution_events(market, fills, spot_rows):
    return [
        (local_time(fill["time"]), {"종목": market, **row, "레버리지": None})
        for fill, row in zip(reversed(fills), spot_rows)
    ]


def execution_events(path, market, fills, spot_rows):
    events = spot_execution_events(market, fills, spot_rows)
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='leverage_positions'"
        ).fetchone()
        positions = db.execute("SELECT * FROM leverage_positions ORDER BY id").fetchall() if exists else []

    for item in positions:
        position = Long(item["margin"], item["leverage"], item["entry"], item["fee"], item["slippage"])
        cash_columns = item.keys()
        opened = local_time(item["opened_at"])
        events.append((opened, {
            "종목": market,
            "체결 시각 (KST)": opened.strftime("%Y-%m-%d %H:%M:%S"),
            "구분": "롱 진입",
            "체결가": position.entry,
            "수량": position.quantity,
            "수수료": round(position.entry_fee, 2),
            "실현 손익": None,
            "체결 후 평균 매입가": position.entry,
            "체결 후 현금": item["entry_cash"] if "entry_cash" in cash_columns else None,
            "레버리지": position.leverage,
        }))
        if item["closed_at"] is not None:
            closed = local_time(item["closed_at"])
            exit_fee = position.quantity * item["exit"] * position.fee if item["status"] == "롱 종료" else 0.0
            events.append((closed, {
                "종목": market,
                "체결 시각 (KST)": closed.strftime("%Y-%m-%d %H:%M:%S"),
                "구분": item["status"],
                "체결가": item["exit"],
                "수량": position.quantity,
                "수수료": round(exit_fee, 2),
                "실현 손익": item["pnl"],
                "체결 후 평균 매입가": None,
                "체결 후 현금": item["exit_cash"] if "exit_cash" in cash_columns else None,
                "레버리지": position.leverage,
            }))
    return events


def session_execution_rows(directory, active_path, active_events):
    events = list(active_events)
    unavailable = 0
    for path in sorted(directory.glob("*.sqlite3")):
        if path == active_path:
            continue
        try:
            account, fills, _ = read_account(path)
            if account is None:
                continue
            settings = json.loads(account["settings"])
            spot_rows = trade_rows(fills, settings["fee"])
            events.extend(execution_events(path, settings["market"], fills, spot_rows))
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            unavailable += 1
    ordered = sorted(reversed(events), key=lambda event: event[0], reverse=True)
    return [row for _, row in ordered], unavailable


def signed_style(value):
    color = direction_color(value) if pd.notna(value) else None
    return f"color: {color}" if color else ""


def signed_text(value, text):
    color = direction_color(value)
    return f'<span style="color:{color}">{escape(text)}</span>' if color else escape(text)


def signed_metric(label, value, text, *, border=False):
    if value is None:
        st.metric(label, text, border=border)
        return
    with st.container(border=border):
        st.caption(label)
        st.markdown(f'<div style="font-size:1.75rem">{signed_text(value, text)}</div>', unsafe_allow_html=True)


def show_trades(rows):
    data = pd.DataFrame(rows).style.map(signed_style, subset=["실현 손익"])
    st.dataframe(
        data, hide_index=True,
        column_config={
            "체결가": st.column_config.NumberColumn(format="localized"),
            "수량": st.column_config.NumberColumn(format="%.8f"),
            "수수료": st.column_config.NumberColumn(format="localized"),
            "체결 후 평균 매입가": st.column_config.NumberColumn(format="localized"),
            "실현 손익": st.column_config.NumberColumn(format="localized"),
            "체결 후 현금": st.column_config.NumberColumn(format="localized"),
            "레버리지": st.column_config.NumberColumn(format="%d배"),
        },
    )


def kst_axis(title, date_only=False, seconds=False):
    label_format = "%y-%m-%d" if date_only else "%H:%M:%S" if seconds else "%m-%d %H:%M"
    return alt.X(
        "시각:T",
        title=title,
        scale=alt.Scale(type="utc"),
        axis=alt.Axis(
            labelExpr=f"utcFormat(toNumber(datum.value) + 9 * 60 * 60 * 1000, '{label_format}')"
        ),
    )


@st.cache_data(ttl=4, show_spinner=False)
def load_market_candles(market, minutes, boundary):
    args = SimpleNamespace(market=market, minutes=minutes)
    return recent_candles(args, boundary + timedelta(minutes=minutes), count=200)


@st.cache_data(ttl=1, show_spinner=False)
def load_second_candles(market, boundary):
    return recent_candles(SimpleNamespace(market=market), boundary + timedelta(seconds=1),
                          count=200, seconds=True)


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


def show_price_chart(candles, fills, settings, quote, boundary, now, count, indicators, average, date_only=False, seconds=False):
    market = settings["market"]
    try:
        name = load_krw_market_names().get(market)
    except (URLError, TimeoutError, ValueError, KeyError, TypeError):
        name = None
    if name is None and market == "KRW-BTC":
        name = "비트코인"
    st.subheader(f"{name} ({market})" if name else market)
    if not candles:
        st.info("확정 봉을 불러오면 가격과 체결 지점이 표시됩니다.")
        return

    time_format = "%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M"
    series = indicator_series(candles, indicators, settings)
    first = max(0, len(candles) - count)
    points = []
    for index in range(first, len(candles)):
        candle = candles[index]
        point = {
            "시각": candle.start.isoformat(),
            "한국 시간": candle.start.astimezone(KST).strftime(time_format),
            "시가": candle.opening,
            "고가": candle.high,
            "저가": candle.low,
            "종가": candle.closing,
            "거래량": candle.volume,
            "상태": "진행 중" if candle.start == boundary else "확정",
        }
        point.update({name: float(values.iloc[index]) for name, values in series.items()
                      if pd.notna(values.iloc[index])})
        points.append(point)

    markers = []
    for fill in fills:
        time = datetime.fromisoformat(fill["time"])
        if candles[first].start <= time <= now:
            markers.append({
                "시각": time.isoformat(),
                "한국 시간": time.astimezone(KST).strftime(time_format),
                "구분": SIDE_LABELS[fill["side"]],
                "체결가": fill["price"],
            })

    x = kst_axis("봉 시작 시각 (KST)", date_only, seconds)
    price_scale = alt.Scale(zero=False)
    source = alt.Chart(alt.Data(values=points)).encode(
        x=x,
        tooltip=["한국 시간:N", "상태:N", *[
            alt.Tooltip(f"{field}:Q", format=",.2f") for field in ("시가", "고가", "저가", "종가", "거래량")
        ]],
    )
    direction = alt.condition(alt.datum["종가"] > alt.datum["시가"], alt.value(POSITIVE), alt.value(NEUTRAL))
    direction["condition"] = [
        direction["condition"],
        {"test": "datum['종가'] < datum['시가']", "value": NEGATIVE},
    ]
    wick = source.mark_rule().encode(
        y=alt.Y("저가:Q", title=quote, scale=price_scale), y2="고가:Q", color=direction
    )
    body = source.mark_bar(size=5).encode(
        y=alt.Y("시가:Q", title=quote, scale=price_scale), y2="종가:Q", color=direction
    )
    flat = source.transform_filter(alt.datum["시가"] == alt.datum["종가"]).mark_tick(
        orient="horizontal", size=5
    ).encode(y=alt.Y("종가:Q", title=quote, scale=price_scale), color=direction)
    overlays = [name for name in series if name not in ("RSI 14", "MACD", "시그널", "히스토그램")]
    palette = {"밴드 상단": "#3975e8", "밴드 중심선": "#4f62dc", "밴드 하단": "#3975e8",
               "SMA 20": "#9252d4", "EMA 20": "#10908e", "매수": "#2c70c1", "매도": "#c63e62"}
    dashes = {"밴드 상단": [7, 4], "밴드 중심선": [2, 4], "밴드 하단": [7, 4],
              "SMA 20": [1, 0], "EMA 20": [10, 3, 2, 3]}
    labels = [*overlays, "매수", "매도"]
    colors = [palette[label] for label in labels]
    color = alt.Color("항목:N", scale=alt.Scale(domain=labels, range=colors), title=None)
    lines = (
        alt.Chart(alt.Data(values=points))
        .transform_fold(overlays, as_=["항목", "가격"])
        .mark_line(strokeWidth=2.8, clip=True)
        .encode(
            x=x,
            y=alt.Y("가격:Q", title=quote, scale=price_scale),
            color=color,
            strokeDash=alt.StrokeDash("항목:N", legend=None,
                scale=alt.Scale(domain=overlays, range=[dashes[name] for name in overlays])),
            tooltip=["한국 시간:N", "항목:N", alt.Tooltip("가격:Q", format=",.2f")],
        )
    )
    trades = (
        alt.Chart(alt.Data(values=markers))
        .transform_calculate(항목="datum['구분']")
        .mark_point(filled=True, size=155, opacity=1, stroke="white", strokeWidth=1.2)
        .encode(
            x=x,
            y=alt.Y("체결가:Q", title=quote, scale=price_scale),
            color=color,
            shape=alt.Shape(
                "구분:N",
                scale=alt.Scale(domain=["매수", "매도"], range=["triangle-up", "triangle-down"]),
                legend=None,
            ),
            tooltip=["한국 시간:N", "구분:N", alt.Tooltip("체결가:Q", format=",.2f")],
        )
    )
    zoom = alt.selection_interval(bind="scales", encodings=["x"], name="chart_zoom")
    layers = [wick.add_params(zoom), body, flat]
    if "볼린저 밴드" in indicators:
        layers.insert(0, alt.Chart(alt.Data(values=points)).mark_area(
            color="#3b82f6", opacity=0.09, clip=True
        ).encode(x=x, y=alt.Y("밴드 하단:Q", title=quote, scale=price_scale), y2="밴드 상단:Q"))
    if overlays:
        layers.append(lines)
    if average is not None:
        layers.append(alt.Chart(alt.Data(values=[{"평균 매입가": average}]))
                      .mark_rule(color="#d97706", strokeDash=[6, 4], strokeWidth=2)
                      .encode(y=alt.Y("평균 매입가:Q", title=quote, scale=price_scale),
                              tooltip=alt.Tooltip("평균 매입가:Q", format=",.2f")))
        st.caption(f"주황색 점선 · 보유 평균 매입가 {average:,.2f} {quote} (매수 수수료 포함)")
    price = alt.layer(*layers, trades).properties(height=460)
    volume = source.mark_bar(size=5).encode(
        y=alt.Y("거래량:Q", title="거래량"), color=direction
    ).properties(height=75)
    panels = [price, volume]
    if "RSI 14" in indicators and "RSI 14" in series:
        guides = alt.Chart(alt.Data(values=[{"기준": 30}, {"기준": 70}])).mark_rule(
            color="#8b73a9", strokeDash=[5, 4]
        ).encode(y=alt.Y("기준:Q", title="RSI 14", scale=alt.Scale(domain=[0, 100])))
        rsi = source.mark_line(color="#9252d4", strokeWidth=2.7).encode(
            y=alt.Y("RSI 14:Q", scale=alt.Scale(domain=[0, 100])),
            tooltip=["한국 시간:N", alt.Tooltip("RSI 14:Q", format=".2f")]
        )
        panels.append((guides + rsi).properties(height=100))
    if "MACD" in indicators:
        histogram = source.mark_bar(size=5, opacity=0.45).encode(
            color={
                "condition": [{"test": "datum['히스토그램'] > 0", "value": POSITIVE},
                              {"test": "datum['히스토그램'] < 0", "value": NEGATIVE}],
                "value": NEUTRAL,
            },
            y=alt.Y("히스토그램:Q", title="MACD"),
            tooltip=["한국 시간:N", alt.Tooltip("히스토그램:Q", format=",.2f")],
        )
        macd = alt.Chart(alt.Data(values=points)).transform_fold(
            ["MACD", "시그널"], as_=["지표", "값"]
        ).mark_line(strokeWidth=2.7).encode(
            x=x, y=alt.Y("값:Q", title="MACD"),
            color=alt.Color("지표:N", scale=alt.Scale(domain=["MACD", "시그널"], range=["#6062de", "#cb741c"])),
            strokeDash=alt.StrokeDash("지표:N", legend=None,
                scale=alt.Scale(domain=["MACD", "시그널"], range=[[1, 0], [6, 4]])),
            tooltip=["한국 시간:N", "지표:N", alt.Tooltip("값:Q", format=",.2f")],
        )
        panels.append((histogram + macd).properties(height=100))
    chart = (
        alt.vconcat(*panels, spacing=12)
        .resolve_scale(x="shared")
        .configure_legend(orient="top", symbolStrokeWidth=3)
    )
    st.altair_chart(chart, width="stretch")
    refresh = " · 1초마다 조회" if seconds else ""
    st.caption(f"최근 봉 {len(points)}개{refresh} · 삼각형은 현물 모의 체결가")


def performance_points(candles, fills, initial_cash, minutes, adjustments=(), *, fee):
    if not candles or not (fills or adjustments):
        return [], None

    trades = iter(sorted([*fills, *adjustments], key=lambda event: event["time"]))
    fill = next(trades)
    first_trade = datetime.fromisoformat(fill["time"])
    cash, units, net_funding = initial_cash, 0.0, 0.0
    points = []
    baseline_equity = None
    for candle in candles:
        close_time = candle.start + timedelta(minutes=minutes)
        if close_time <= first_trade:
            continue

        while fill is not None and datetime.fromisoformat(fill["time"]) < candle.start:
            if "amount" in fill.keys():
                cash += fill["amount"]
                net_funding += fill["amount"]
            else:
                quantity_change = fill["units"] - units
                gross = quantity_change * fill["price"]
                cash -= gross + abs(gross) * fee
                units = fill["units"]
            fill = next(trades, None)

        if baseline_equity is None:
            baseline_equity = cash + units * candle.opening
            baseline_price = candle.opening
            starting_funding = net_funding
            peak = baseline_equity
            drawdown = 0.0
            points.append({
                "시각": candle.start.isoformat(),
                "한국 시간": candle.start.astimezone(KST).strftime("%Y-%m-%d %H:%M"),
                "누적 수익률": 100 * ((baseline_equity - net_funding) / initial_cash - 1) if initial_cash else None,
                "모의 전략": 0.0,
                "BTC 보유": 0.0,
            })

        while fill is not None and datetime.fromisoformat(fill["time"]) < close_time:
            if "amount" in fill.keys():
                cash += fill["amount"]
                net_funding += fill["amount"]
            else:
                quantity_change = fill["units"] - units
                gross = quantity_change * fill["price"]
                cash -= gross + abs(gross) * fee
                units = fill["units"]
            fill = next(trades, None)

        equity = cash + units * candle.closing
        adjusted = equity - (net_funding - starting_funding)
        peak = max(peak, adjusted)
        if peak > 0:
            drawdown = max(drawdown, 100 * (1 - adjusted / peak))
        points.append({
            "시각": close_time.isoformat(),
            "한국 시간": close_time.astimezone(KST).strftime("%Y-%m-%d %H:%M"),
            "누적 수익률": 100 * ((equity - net_funding) / initial_cash - 1) if initial_cash else None,
            "모의 전략": 100 * (adjusted / baseline_equity - 1) if baseline_equity > 0 else None,
            "BTC 보유": 100 * (candle.closing / baseline_price - 1),
        })
    return (points, drawdown) if points else ([], None)


def show_performance(candles, fills, rows, settings, quote, adjustments):
    st.subheader("현물 모의매매 성과")
    profits = [row["실현 손익"] for row in rows if row["실현 손익"] is not None]
    completed = len(profits)
    average = sum(profits) / completed if completed else None
    win_rate = f"{sum(profit > 0 for profit in profits) / completed:.1%}" if completed else "—"
    total, mean_profit, win = st.columns(3)
    with total:
        signed_metric("누적 실현 손익", sum(profits), f"{sum(profits):+,.2f} {quote}")
    with mean_profit:
        signed_metric("매도 체결당 평균 손익", average, f"{average:+,.2f} {quote}" if average is not None else "—")
    win.metric(f"승률 · 매도 {completed:,}건", win_rate)

    points, drawdown = performance_points(candles, fills, settings["cash"], settings["minutes"], adjustments,
                                          fee=settings["fee"])
    if not points:
        st.info("첫 거래 또는 현금 조정 이후 확정 봉이 들어오면 수익률 추이를 표시합니다.")
        return

    equity_tab, comparison_tab = st.tabs(["초기 자금 대비", "BTC 보유 비교"])
    for tab, fields in ((equity_tab, ["누적 수익률"]), (comparison_tab, ["모의 전략", "BTC 보유"])):
        with tab:
            chart = (
                alt.Chart(alt.Data(values=points))
                .transform_fold(fields, as_=["구분", "수익률"])
                .mark_line()
                .encode(
                    x=kst_axis("시각 (KST)"),
                    y=alt.Y("수익률:Q", title="수익률 (%)", scale=alt.Scale(zero=False)),
                    color=alt.Color("구분:N", title=None),
                    tooltip=["한국 시간:N", "구분:N", alt.Tooltip("수익률:Q", format="+.4f")],
                )
                .properties(height=230)
            )
            st.altair_chart(chart, width="stretch")
            if len(fields) == 2:
                st.caption("같은 시작 시점의 수익률을 비교합니다. 전략은 현금 조정을 제외하고 BTC 보유는 거래 비용 없이 계산합니다.")
    st.caption(f"표시 구간 최대 낙폭 {drawdown:.2f}% · 확정 봉 종가 기준")


def show_leverage_performance(market, events, totals, quote):
    st.subheader("레버리지 매매 성과")
    st.caption(market)
    if totals is None:
        st.info("레버리지 계좌를 불러오지 못했습니다.")
        return

    closed = [
        (timestamp, row) for timestamp, row in events
        if row["구분"] in LONG_TRADE_LABELS[1:] and row["실현 손익"] is not None
    ]
    completed = len(closed)
    wins = sum(row["실현 손익"] > 0 for _, row in closed)
    liquidations = sum(row["구분"] == "강제청산" for _, row in closed)
    profit, win_rate, exits = st.columns(3)
    with profit:
        signed_metric("누적 실현 손익", totals.realized_pnl, f"{totals.realized_pnl:+,.2f} {quote}")
    win_rate.metric("승률", f"{wins / completed:.1%}" if completed else "—")
    exits.metric("종료 포지션", f"{completed:,}건", help=f"강제청산 {liquidations:,}건")
    st.caption(f"롱 진입 {totals.entries:,}건 · 강제청산 {liquidations:,}건 · "
               f"누적 수수료 {totals.fees:,.2f} {quote}")
    if not closed:
        st.info("롱 포지션이 종료되면 실현 손익 추이를 표시합니다.")
        return

    points = []
    cumulative = 0.0
    for timestamp, row in sorted(closed, key=lambda event: event[0]):
        cumulative += row["실현 손익"]
        points.append({
            "시각": timestamp.astimezone(timezone.utc).isoformat(),
            "한국 시간": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "누적 실현 손익": cumulative,
            "구분": row["구분"],
        })
    chart = alt.Chart(alt.Data(values=points)).mark_line(point=True).encode(
        x=kst_axis("종료 시각 (KST)"),
        y=alt.Y("누적 실현 손익:Q", title=f"누적 실현 손익 ({quote})", axis=alt.Axis(format=",.2~f")),
        tooltip=["한국 시간:N", "구분:N", alt.Tooltip("누적 실현 손익:Q", format="+,.2f")],
    ).properties(height=220)
    st.altair_chart(chart, width="stretch")


def show_strategy(settings, quote):
    st.subheader("전략 설정")
    st.markdown(
        f"**볼린저 밴드** · {settings['period']}개 봉 / "
        f"표준편차 {settings['deviations']:g}배"
    )
    st.markdown(f"**초기 자금** · {settings['cash']:,.2f} {quote}")
    st.markdown(f"**매수 배분** · 가용 현금의 {settings['allocation']:.2%}")
    st.markdown(
        f"**편도 거래 비용** · 수수료 {settings['fee']:.2%} / "
        f"슬리피지 {settings['slippage']:.2%}"
    )
    threshold = settings.get("ml_threshold")
    if threshold is not None:
        st.markdown(f"**ML 매수 필터 임계값** · {threshold:g}")


def show_manual_order(path, account, args, live, quote, asset):
    with st.container(border=True):
        st.subheader("수동 모의매매")
        if live is not None:
            st.metric("최근 업비트 체결가", f"{live.closing:,.2f} {quote}")
        order_type = st.radio("주문 방식", ("시장가", "지정가"), horizontal=True, key=f"{path}:order-type")
        is_limit = order_type == "지정가"
        reference = live.closing if live is not None else None
        order_args = args
        if is_limit:
            reference = st.number_input(f"지정가 ({quote})", min_value=0.0, value=None,
                                        format="%.8f", key=f"{path}:limit-price")
            order_args = SimpleNamespace(**{**vars(args), "slippage": 0.0})
        elif reference is None:
            st.info("업비트 가격이 들어오면 시장가 주문을 할 수 있습니다.")
        choices = st.columns(2)
        side = choices[0].radio("거래 방향", ("BUY", "SELL"), format_func=SIDE_LABELS.get,
                                horizontal=True, key=f"{path}:side-order")
        units = {"quantity": f"{asset} 수량",
                 "percent": "가용 현금 비율 (%)" if side == "BUY" else "보유 수량 비율 (%)"}
        if side == "BUY":
            units["cash"] = f"총 주문 금액 ({quote}, 수수료 포함)"
        unit = choices[1].selectbox("입력 단위", list(units), format_func=units.get,
                                    key=f"{path}:{side}:order-unit")
        if unit == "cash":
            size = amount_input(units[unit], key=f"{path}:{side}:{unit}:grouped-order-size") or 0.0
        else:
            step, precision = (0.0001, "%.8f") if unit == "quantity" else (5.0, "%.2f")
            size = st.number_input(units[unit], min_value=0.0, step=step, format=precision,
                                   key=f"{path}:{side}:{unit}:order-size")
        if unit == "percent":
            presets = st.columns(4)
            for column, percent in zip(presets, (25, 50, 75, 100)):
                column.button(f"{percent}%", key=f"{path}:{side}:{percent}:order-preset",
                              on_click=st.session_state.__setitem__,
                              args=(f"{path}:{side}:{unit}:order-size", float(percent)))

        order_account = Account(account["cash"], account["units"])
        if side == "BUY" and reference:
            max_buy = account["cash"] / (reference * (1 + order_args.slippage) * (1 + args.fee))
            st.caption(f"매수 가능 약 {max_buy:,.8f} {asset}")
        if side == "SELL":
            st.caption(f"매도 가능 {account['units']:,.8f} {asset}")
        estimate = None
        if size > 0 and reference:
            try:
                estimate = order_account.order_quote(side, reference, order_args, size=size, unit=unit)
            except ValueError as error:
                st.warning(str(error))

        if estimate is not None:
            quantity, price, gross, fee = estimate
            total = gross + fee if side == "BUY" else gross - fee
            quantity_col, total_col = st.columns(2)
            quantity_col.metric("예상 체결 수량", f"{quantity:,.8f} {asset}")
            total_col.metric("예상 지출" if side == "BUY" else "예상 수령액", f"{total:,.2f} {quote}")
            st.caption(f"예상 체결가 {price:,.2f} {quote} · 수수료 {fee:,.2f} {quote}")

        action = "지정가 등록" if is_limit else "체결"
        if st.button(f"모의 {SIDE_LABELS[side]} {action}", key=f"{path}:{side}:submit",
                     type="primary", disabled=estimate is None) and estimate is not None:
            try:
                if is_limit:
                    paper_limit_orders(path, order=(side, reference, size, unit))
                    notice = f"모의 {SIDE_LABELS[side]} 지정가 등록 · {reference:,.8g} {quote}"
                else:
                    price = manual_fill(path, side, reference, size=size, unit=unit)
                    notice = f"모의 {SIDE_LABELS[side]} 체결 · {price:,.2f} {quote}"
            except (sqlite3.Error, ValueError) as error:
                st.error(f"모의 체결 실패 · {error}")
            else:
                st.session_state[f"{path}:notice"] = notice
                st.rerun(scope="app")

        notice = st.session_state.pop(f"{path}:notice", None)
        if notice:
            st.success(notice)


def load_account_market(settings):
    minutes = settings["minutes"]
    now = datetime.now(timezone.utc)
    seconds = int(now.timestamp())
    boundary = datetime.fromtimestamp(seconds - seconds % (minutes * 60), timezone.utc)
    return now, boundary, load_market_candles(settings["market"], minutes, boundary)


@st.cache_data(ttl=900, show_spinner=False)
def load_krw_market_names():
    with urlopen("https://api.upbit.com/v1/market/all", timeout=5) as response:
        return {row["market"]: row["korean_name"] for row in json.load(response)
                if row["market"].startswith("KRW-")}


@st.cache_data(ttl=60, show_spinner=False)
def load_krw_tickers():
    with urlopen("https://api.upbit.com/v1/ticker/all?quote_currencies=KRW", timeout=5) as response:
        return [row for row in json.load(response) if row["market"].startswith("KRW-")]


def base_account_path():
    path = account_directory() / "paper.sqlite3"
    if not path.exists():
        settings = dict(market="KRW-BTC", minutes=15, period=20, deviations=2,
                        cash=1_000_000, allocation=0.1, fee=0.0005, slippage=0.0005)
        with closing(sqlite3.connect(path)) as db, db:
            initialize_account(db, settings, settings["cash"])
    return path


def market_account_path(market):
    if not re.fullmatch(r"KRW-[A-Z0-9]+", market):
        raise ValueError("지원하지 않는 원화 종목입니다.")
    base_path = base_account_path()
    account, _, _ = read_account(base_path)
    settings = json.loads(account["settings"])
    if settings["market"] == market:
        return base_path
    path = base_path.parent / f"paper-{market}.sqlite3"
    if not path.exists():
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            initialize_account(db, {**settings, "market": market, "cash": 0.0}, 0.0)
    return path


def clear_unused_market_seeds(directory):
    """Remove previously issued automatic seed cash from unused market accounts."""
    for path in sorted(directory.glob("paper-KRW-*.sqlite3")):
        if not re.fullmatch(r"paper-KRW-[A-Z0-9]+\.sqlite3", path.name):
            continue
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT settings, cash, units, trades FROM account WHERE id=1").fetchone()
            if row is None:
                continue
            merge_legacy_cash(db)
            raw, cash, units, trades = db.execute(
                "SELECT settings, cash, units, trades FROM account WHERE id=1"
            ).fetchone()
            settings = json.loads(raw)
            if not cash or cash != settings["cash"] or units or trades:
                continue
            tables = {name for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if any(table in tables and db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                   for table in ("cash_adjustments", "leverage_positions", "limit_orders")):
                continue
            db.execute("UPDATE account SET settings=?, cash=0 WHERE id=1",
                       (json.dumps({**settings, "cash": 0.0}, sort_keys=True),))


@st.fragment(run_every="60s")
def show_krw_market():
    if "next-market" in st.session_state:
        st.rerun(scope="app")
    st.subheader("업비트 원화 마켓")
    if "market-error" in st.session_state:
        st.error(st.session_state.pop("market-error"))
    query = st.text_input("코인 검색", placeholder="예: 비트코인, BTC").strip().casefold()
    try:
        tickers = load_krw_tickers()
    except (URLError, TimeoutError, ValueError, KeyError, TypeError):
        st.info("원화 마켓 시세를 불러오지 못했습니다. 1분 후 다시 조회합니다.")
        return

    try:
        names = load_krw_market_names()
    except (URLError, TimeoutError, ValueError, KeyError, TypeError):
        names = {}

    rows = []
    for ticker in sorted(tickers, key=lambda row: row["acc_trade_price_24h"], reverse=True):
        code = ticker["market"]
        symbol = code.removeprefix("KRW-")
        name = names.get(code, symbol)
        if query not in f"{name} {code}".casefold():
            continue
        change = round(100 * ticker["signed_change_rate"], 2)
        turnover = ticker["acc_trade_price_24h"] / 1e8
        rows.append({
            "code": code,
            "name": name,
            "symbol": symbol,
            "price": f"{ticker['trade_price']:,.10f}".rstrip("0").rstrip("."),
            "change": f"{change:+.2f}%" if change else "0.00%",
            "direction": "up" if change > 0 else "down" if change < 0 else "",
            "turnover": f"{turnover:,.1f}억" if turnover >= 1 else f"{turnover * 1e4:,.0f}만",
        })

    st.caption(f"{len(rows):,}종목 · 24시간 거래대금순")
    if not rows:
        st.info("검색 결과가 없습니다.")
        return
    selected = st.session_state.get("active-market", "KRW-BTC")
    result = render_market_grid(key="krw-market-grid", data={"rows": rows, "selected": selected},
                                on_selected_change=lambda: None)
    if result.selected != selected and any(row["code"] == result.selected for row in rows):
        st.session_state["next-market"] = result.selected
        st.rerun(scope="app")


def show_leverage_order(path, settings, wallet, mark, quote, portfolio_cash):
    with st.container(border=True):
        st.subheader("레버리지 롱")
        st.caption("전체 포트폴리오 가용 현금 사용 · 격리 증거금 · 시장가")
        if wallet is None:
            return
        _, position, history, _, _ = wallet
        cash = portfolio_cash if portfolio_cash is not None else 0.0
        st.metric("공통 가용 현금", f"{cash:,.2f} {quote}" if portfolio_cash is not None else "계좌 조회 필요")
        st.caption("선택한 계좌 화면에서 최근 체결가가 모의 청산가에 닿으면 증거금을 잃습니다.")
        if mark is None:
            st.info("최근 10초 내 체결가가 들어오면 주문할 수 있습니다.")
        else:
            st.caption(f"최근 체결가 {mark:,.2f} {quote}")

        order = None
        if position is None:
            leverage = st.slider("레버리지 배율", 1, 50, 5, format="%d배", key=f"{path}:leverage")
            margin = amount_input(f"격리 증거금 ({quote})", key=f"{path}:grouped-margin")
            fee, slippage = settings["fee"], settings["slippage"]
            max_margin = max(0.0, cash / (1 + leverage * fee))
            st.caption(f"가용 현금 기준 최대 증거금 {max_margin:,.2f} {quote} · 진입 수수료 반영")
            preview = Long(margin, leverage, mark * (1 + slippage), fee, slippage) if margin and mark else None
            if preview is not None:
                st.metric("포지션 금액", f"{preview.notional:,.2f} {quote}")
                st.metric("예상 모의 청산가", f"{preview.liquidation_price:,.2f} {quote}" if leverage > 1 else "—")
                st.caption(f"예상 진입가 {preview.entry:,.2f} {quote} · 진입 수수료 {preview.entry_fee:,.2f} {quote}")
                required = preview.margin + preview.entry_fee
                if required > cash:
                    st.warning(f"증거금과 진입 수수료에 {required:,.2f} {quote}가 필요합니다.")
                elif mark <= preview.liquidation_price:
                    st.warning("현재가가 예상 모의 청산가 이하입니다. 레버리지 배율을 낮추세요.")
                else:
                    order = (margin, leverage, fee, slippage)
            submit = st.button("모의 롱 진입", type="primary", disabled=order is None, key=f"{path}:long-open")
        else:
            st.caption(f"{position.leverage}배 롱 · 진입가 {position.entry:,.2f} {quote}")
            st.metric("격리 증거금", f"{position.margin:,.2f} {quote}")
            st.metric("모의 청산가", f"{position.liquidation_price:,.2f} {quote}" if position.leverage > 1 else "—")
            if mark is not None:
                pnl = position.release(mark) - position.margin - position.entry_fee
                signed_metric("예상 순손익", pnl, f"{pnl:+,.2f} {quote} ({pnl / position.margin:+.2%})")
            submit = st.button("모의 롱 종료", disabled=mark is None, key=f"{path}:long-close")

        if submit:
            try:
                _, _, _, event, _ = paper_long(
                    path, mark=mark, open_long=order, close_long=position is not None,
                    portfolio_directory=base_account_path().parent,
                )
            except (sqlite3.Error, ValueError) as error:
                st.error(f"레버리지 주문 실패 · {error}")
            else:
                if event:
                    st.session_state[f"{path}:leverage-notice"] = event
                st.rerun()

        notice = st.session_state.pop(f"{path}:leverage-notice", None)
        if notice:
            st.success(notice)
        if history:
            with st.expander("레버리지 최근 종료 내역"):
                for timestamp, leverage, entry, exit_price, pnl, status in history:
                    amount = signed_text(pnl, f"{pnl:+,.2f} {quote}")
                    st.caption(f"{local_time(timestamp):%m-%d %H:%M:%S} · {leverage}배 · {status} · {amount}",
                               unsafe_allow_html=True)
                    st.caption(f"진입 {entry:,.2f} → 종료 {exit_price:,.2f} {quote}")


def show_order_and_account(path, account, settings, args, live, quote, asset, orders, wallet, mark, portfolio_cash):
    mode = st.radio("모의매매 유형", ("현물", "레버리지 롱"), horizontal=True, key=f"{path}:trade-mode")
    if mode == "레버리지 롱":
        show_leverage_order(path, settings, wallet, mark, quote, portfolio_cash)
    else:
        show_manual_order(path, account, args, live, quote, asset)

    if orders:
        with st.expander("지정가 주문 내역", expanded=True):
            labels = {"OPEN": "미체결", "FILLED": "체결", "CANCELED": "취소", "REJECTED": "잔고 부족"}
            for order in orders:
                st.caption(f"{SIDE_LABELS[order['side']]} · {order['quantity']:,.8g} {asset} · "
                           f"{order['price']:,.8g} {quote} · {labels[order['status']]}")
                if order["status"] == "OPEN" and st.button("취소", key=f"{path}:cancel:{order['id']}"):
                    paper_limit_orders(path, cancel=order["id"])
                    st.rerun(scope="app")

    with st.expander("가용 현금 및 계좌 관리"):
        st.caption(f"{settings['market']} 선택 계좌 가용 현금 {account['cash']:,.2f} {quote}")
        target = amount_input(f"변경할 전체 가용 현금 ({quote})", key="portfolio-cash-target")
        if st.button("전체 가용 현금 변경", disabled=target is None, key="apply-portfolio-cash"):
            try:
                adjust_portfolio_cash(base_account_path().parent, path, target)
            except (sqlite3.Error, ValueError) as error:
                st.error(str(error))
            else:
                st.session_state[f"{path}:cash-changed"] = True
                st.rerun(scope="app")
        if st.session_state.pop(f"{path}:cash-changed", False):
            st.success("전체 가용 현금을 변경했습니다.")

        base_path = base_account_path()
        paths = [base_path, *sorted(path.parent.glob("*.sqlite3"))]
        paths = list(dict.fromkeys([*paths, path]))
        if len(paths) > 1:
            selected = st.selectbox(
                "모의계좌", paths, index=paths.index(path), key=f"{path}:account-picker",
                format_func=lambda item: "기본 계좌" if item == base_path else f"모의계좌 · {item.stem}",
            )
            if selected != path:
                st.session_state["next-paper-db"] = str(selected)
                st.rerun(scope="app")
        st.caption("새 계좌에서 빈 체결 내역으로 시작할 수 있습니다.")
        starting_cash = amount_input(f"새 계좌 초기 자금 ({quote})", minimum=0.01,
                                    value=float(settings["cash"]), key=f"{path}:grouped-new-cash")
        if st.button("새 모의계좌 시작", disabled=starting_cash is None, key=f"{path}:new-account"):
            try:
                fd, filename = mkstemp(prefix="paper-", suffix=".sqlite3", dir=path.parent)
                os.close(fd)
                new_path = Path(filename)
                try:
                    with closing(sqlite3.connect(new_path)) as db, db:
                        initialize_account(db, {**settings, "cash": starting_cash}, starting_cash)
                except sqlite3.Error:
                    new_path.unlink(missing_ok=True)
                    raise
            except (OSError, sqlite3.Error) as error:
                st.error(f"새 모의계좌를 만들 수 없습니다 · {error}")
            else:
                st.session_state["next-paper-db"] = str(new_path)
                st.rerun(scope="app")


def portfolio_totals(directory, active_path, account, settings, rows, adjustments, wallet, prices):
    totals = dict(cash=0.0, initial=0.0, funding=0.0, trades=0, fees=0.0,
                  realized=0.0, position_value=0.0, unrealized=0.0)
    missing_markets = set()
    unavailable = 0
    for path in sorted({active_path, *directory.glob("*.sqlite3")}):
        try:
            if path == active_path:
                current, config, trades, funding, long_wallet = account, settings, rows, adjustments, wallet
            else:
                current, fills, funding = read_account(path)
                if current is None:
                    continue
                config = json.loads(current["settings"])
                trades = trade_rows(fills, config["fee"])
                long_wallet = paper_long(path)
            if long_wallet is None:
                unavailable += 1
                continue
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            unavailable += 1
            continue

        cash, long_position, _, _, long_totals = long_wallet
        totals["cash"] += cash
        totals["initial"] += config["cash"]
        totals["funding"] += sum(item["amount"] for item in funding)
        totals["trades"] += current["trades"] + long_totals.entries + long_totals.exits
        totals["fees"] += sum(row["수수료"] for row in trades) + long_totals.fees
        totals["realized"] += sum(row["실현 손익"] or 0.0 for row in trades) + long_totals.realized_pnl

        if not current["units"] and long_position is None:
            continue
        market = config["market"]
        price = prices.get(market)
        if price is None:
            missing_markets.add(market)
            continue
        spot_value = current["units"] * price
        spot_average = (trades[0]["체결 후 평균 매입가"] or 0.0) if trades else 0.0
        totals["position_value"] += spot_value
        totals["unrealized"] += spot_value - current["units"] * spot_average
        if long_position is not None:
            long_value = long_position.release(price)
            totals["position_value"] += long_value
            totals["unrealized"] += long_value - long_position.margin - long_position.entry_fee
    return totals, missing_markets, unavailable


def show_portfolio(directory, path, account, settings, rows, adjustments, wallet):
    st.subheader("전체 포트폴리오")
    try:
        prices = {ticker["market"]: float(ticker["trade_price"]) for ticker in load_krw_tickers()}
    except (URLError, TimeoutError, ValueError, KeyError, TypeError):
        prices = {}
    totals, missing, unavailable = portfolio_totals(directory, path, account, settings, rows,
                                                    adjustments, wallet, prices)
    cash, count, fees, realized = st.columns(4)
    equity, performance, open_profit = st.columns(3)
    if unavailable:
        st.warning(f"계좌 {unavailable:,}개의 데이터를 불러오지 못해 전체 집계를 표시할 수 없습니다.")
    status = "계좌 조회 필요" if unavailable else "시세 필요" if missing else None
    cash.metric("가용 현금", "—" if unavailable else f"{totals['cash']:,.2f} KRW", border=True)
    count.metric("누적 체결", "—" if unavailable else f"{totals['trades']:,}건", border=True)
    fees.metric("누적 추정 수수료", "—" if unavailable else f"{totals['fees']:,.2f} KRW", border=True)
    with realized:
        signed_metric("누적 실현 손익", None if unavailable else totals["realized"],
                      "—" if unavailable else f"{totals['realized']:+,.2f} KRW", border=True)
    if status:
        equity.metric("평가자산", status, border=True)
        performance.metric("현금 조정 제외 수익률", status, border=True)
        open_profit.metric("보유 평가손익", status, border=True)
        if missing:
            st.caption(f"시세를 불러오지 못한 종목 · {', '.join(sorted(missing))}")
    else:
        total_equity = totals["cash"] + totals["position_value"]
        return_pct = 100 * ((total_equity - totals["funding"]) / totals["initial"] - 1)
        equity.metric("평가자산", f"{total_equity:,.2f} KRW", border=True)
        with performance:
            signed_metric("현금 조정 제외 수익률", return_pct, f"{return_pct:+.4f}%", border=True)
        with open_profit:
            signed_metric("보유 평가손익", totals["unrealized"], f"{totals['unrealized']:+,.2f} KRW", border=True)
    st.caption("내 모든 모의계좌 합산 · 평가 시세는 원화 마켓 기준으로 최대 1분간 캐시됩니다.")
    return totals["cash"] if not unavailable else None


@st.fragment(run_every="1s")
def show_account(path, config, *, guest):
    if guest:
        render_account(path, guest=True)
    else:
        with show_storage_error(), account_changes(config):
            render_account(path, guest=False)


def show_guest_actions():
    with st.container(border=True):
        st.subheader("모의 주문")
        for label in ("현물 매수", "현물 매도", "레버리지 롱 진입", "가용 현금 및 계좌 관리"):
            if st.button(label, width="stretch"):
                st.session_state["show-login-dialog"] = True
                st.rerun(scope="app")


def render_account(path, *, guest):
    if not path.is_file():
        st.info("계좌 파일이 없습니다. 모의매매 에이전트를 한 번 실행해 기본 계좌를 만드세요.")
        return
    try:
        account, fills, adjustments = read_account(path)
    except sqlite3.Error as error:
        st.error(f"모의매매 DB를 읽을 수 없습니다. {error}")
        return
    if account is None:
        st.info("이 DB에 초기화된 모의매매 계좌가 없습니다.")
        return

    settings = json.loads(account["settings"])
    market = settings["market"]
    quote, asset = market.split("-", maxsplit=1)
    rows = trade_rows(fills, settings["fee"])
    st.markdown(f"""
<div style="background: linear-gradient(115deg, #18264e, #273882 58%, #0e6266); color: #fff;
            border-radius: 18px; padding: 24px 28px; margin-bottom: 8px;">
  <div style="font-size: 0.82rem; color: #bdeaff; font-weight: 650;">모의매매</div>
  <div style="font-size: clamp(1.8rem, 5vw, 2.8rem); font-weight: 740;">{escape(market)}</div>
  <div style="color: #e0eaff;">볼린저 밴드 · {int(settings['minutes'])}분봉</div>
</div>
""", unsafe_allow_html=True)

    last = account["last"]
    if last:
        processed = local_time(last)
        age = datetime.now(KST) - processed
        status = "최근 봉 처리 기록 지연 · " if age >= timedelta(minutes=2 * settings["minutes"]) else ""
        st.caption(f"{status}마지막 처리 봉 {processed:%Y-%m-%d %H:%M} KST")
    else:
        st.caption("처리된 봉 없음")

    args = SimpleNamespace(**{"ml_threshold": None, **settings})
    try:
        now, boundary, market_candles = load_account_market(settings)
    except (URLError, TimeoutError, ValueError, KeyError, TypeError):
        now = datetime.now(timezone.utc)
        interval = args.minutes * 60
        seconds = int(now.timestamp())
        boundary = datetime.fromtimestamp(seconds - seconds % interval, timezone.utc)
        market_candles = []
        st.warning("업비트 공개 시세를 불러오지 못했습니다. 잠시 후 다시 조회합니다.")

    seconds = int(now.timestamp())
    candles = [candle for candle in market_candles if candle.start < boundary]
    latest = market_candles[-1] if market_candles else None
    live = latest if latest is not None and latest.start == boundary else None
    average = rows[0]["체결 후 평균 매입가"] if rows else None
    cost = (average or 0) * account["units"]
    second_boundary = now.replace(microsecond=0)
    second_candles = []
    wallet = None
    mark = None
    try:
        if not guest:
            wallet = paper_long(path)
        if ((wallet is not None and wallet[1] is not None) or st.session_state.get(f"{path}:trade-mode") == "레버리지 롱"
                or st.session_state.get(f"{path}:chart-interval") == 0):
            second_candles = load_second_candles(market, second_boundary)
            if second_candles and timedelta(0) <= datetime.now(timezone.utc) - second_candles[-1].start < timedelta(seconds=10):
                mark = second_candles[-1].closing
            if wallet is not None and wallet[1] is not None and mark is not None and mark <= wallet[1].liquidation_price:
                wallet = paper_long(path, mark=mark)
                if wallet[3]:
                    st.warning("레버리지 롱이 모의 강제청산됐습니다.")
    except (URLError, TimeoutError, ValueError, KeyError, TypeError):
        pass
    except sqlite3.Error:
        wallet = None
        st.warning("레버리지 모의계좌를 불러올 수 없습니다.")

    if not guest:
        if wallet is not None:
            account = dict(account, cash=wallet[0])
        try:
            orders = paper_limit_orders(path, market_price=live.closing if live else None)
            current, _, adjustments = read_account(path)
            if current["trades"] != account["trades"]:
                st.rerun(scope="app")
            account = dict(current)
            if wallet is not None:
                wallet = (account["cash"], *wallet[1:])
        except sqlite3.Error as error:
            orders = []
            st.warning(f"지정가 주문을 확인할 수 없습니다 · {error}")
        net_funding = sum(event["amount"] for event in adjustments)
        if account["cash"] < 0:
            st.warning("기존 롱 포지션과 손익을 반영한 가용 현금이 음수입니다. 가용 현금을 충전하세요.")

        portfolio_cash = show_portfolio(base_account_path().parent, path, account, settings, rows, adjustments, wallet)
        average_price = f"{average:,.2f} {quote}" if average is not None else "—"
        st.caption(f"{market} 선택 계좌 · 가용 현금 {account['cash']:,.2f} {quote} · "
                   f"현물 {account['units']:,.8f} {asset} · 평균 매입가 {average_price}")

        if account["units"] > 0:
            with st.expander("청산 가격별 예상 손익"):
                exit_factor = (1 - args.slippage) * (1 - args.fee)
                break_even = cost / (account["units"] * exit_factor)
                st.caption(f"비용 포함 손익분기 시세 약 {break_even:,.2f} {quote}")
                target = st.number_input(f"가정하는 청산 시세 ({quote})", min_value=0.0,
                                         value=None, key=f"{path}:exit-price")
                if target:
                    proceeds = target * account["units"] * exit_factor
                    proceeds_col, pnl_col = st.columns(2)
                    proceeds_col.metric("전량 청산 예상 수령액", f"{proceeds:,.2f} {quote}")
                    with pnl_col:
                        signed_metric("예상 순손익", proceeds - cost, f"{proceeds - cost:+,.2f} {quote}")

    with st.container(key="market-workspace"):
        chart_panel, order_panel = st.columns([3, 1], gap="medium")
    with chart_panel.container(border=True, width="stretch", key="market-chart-host"):
        interval_col, count_col, overlay_col = st.columns(3)
        intervals = (0, 1, 3, 5, 10, 15, 30, 60, 240, 1440, 10080)
        chart_minutes = interval_col.selectbox("차트 간격", intervals,
                                               index=intervals.index(args.minutes),
                                               format_func=lambda value: {0: "1초봉", 1440: "일봉", 10080: "주봉"}.get(value, f"{value}분"),
                                               key=f"{path}:chart-interval")
        count = count_col.selectbox("표시 봉 수", (30, 60, 100, 200), index=2,
                                    key=f"{path}:chart-count")
        indicators = overlay_col.multiselect("차트 지표", INDICATORS, default=[],
                                              key=f"{path}:chart-indicators")
        if chart_minutes == 0:
            chart_boundary = second_boundary
        elif chart_minutes == 10080:
            chart_boundary = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())
        else:
            interval = chart_minutes * 60
            chart_boundary = datetime.fromtimestamp(seconds - seconds % interval, timezone.utc)
        chart_candles = market_candles
        if chart_minutes == 0:
            chart_candles = second_candles
        elif chart_minutes != args.minutes:
            try:
                chart_candles = load_market_candles(market, chart_minutes, chart_boundary)
            except (URLError, TimeoutError, ValueError, KeyError, TypeError):
                chart_candles = []
        show_price_chart(chart_candles, fills, settings, quote, chart_boundary, now, count, indicators, average,
                         date_only=chart_minutes >= 1440, seconds=chart_minutes == 0)
        base, symbol = market.split("-", maxsplit=1)
        st.link_button("업비트 원본 차트 열기", f"https://upbit.com/exchange?code=CRIX.UPBIT.{base}-{symbol}")

    if guest:
        with order_panel:
            show_guest_actions()
        show_strategy(settings, quote)
        return

    with order_panel:
        show_order_and_account(path, account, settings, args, live, quote, asset, orders, wallet, mark, portfolio_cash)

    try:
        active_events = execution_events(path, market, fills, rows)
    except sqlite3.Error:
        st.warning("선택 종목의 레버리지 체결 내역을 불러오지 못했습니다.")
        active_events = spot_execution_events(market, fills, rows)

    chart, strategy = st.columns([2, 1])
    with chart:
        with st.container(border=True):
            show_performance(candles, fills, rows, settings, quote, adjustments)
        with st.container(border=True):
            show_leverage_performance(market, active_events, wallet[4] if wallet is not None else None, quote)
    with strategy.container(border=True):
        show_strategy(settings, quote)

    if adjustments:
        with st.expander("가용 현금 변경 내역"):
            st.caption("순조정 " + signed_text(net_funding, f"{net_funding:+,.2f} {quote}"), unsafe_allow_html=True)
            amount = f"변경액 ({quote})"
            history = pd.DataFrame([{
                "변경 시각 (KST)": local_time(event["time"]).strftime("%Y-%m-%d %H:%M:%S"),
                amount: event["amount"],
            } for event in reversed(adjustments)])
            st.dataframe(history.style.map(signed_style, subset=[amount]), hide_index=True)

    st.subheader("체결 내역")
    executions, unavailable = session_execution_rows(base_account_path().parent, path, active_events)
    if unavailable:
        st.warning(f"일부 계좌의 체결 내역을 불러오지 못했습니다. · {unavailable:,}개")
    if not executions:
        st.info("아직 체결 내역이 없습니다. 모의매매 조건이 충족되면 여기에 표시됩니다.")
        return

    recent, history = st.tabs(["최근 체결", "전체 이력"])
    with recent:
        st.caption(f"마지막 체결 {executions[0]['체결 시각 (KST)']} KST · 전체 종목 · 금액 단위 KRW")
        show_trades(executions[:5])
    with history:
        show_history(executions)


def show_history(rows):
    side_col, start_col, end_col = st.columns(3)
    side = side_col.selectbox("매매 구분", ("전체", *SIDE_LABELS.values(), *LONG_TRADE_LABELS), key="execution-history:side")
    start = start_col.date_input("시작일", value=None, key="execution-history:start")
    end = end_col.date_input("종료일", value=None, key="execution-history:end")
    if start and end and start > end:
        st.warning("종료일은 시작일 이후로 지정하세요.")
        return

    filtered = [
        row for row in rows
        if (side == "전체" or row["구분"] == side)
        and (start is None or row["체결 시각 (KST)"][:10] >= start.isoformat())
        and (end is None or row["체결 시각 (KST)"][:10] <= end.isoformat())
    ]
    st.caption(f"{len(filtered):,}건 · 날짜는 한국 시간 기준입니다.")
    if filtered:
        show_trades(filtered)
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=list(filtered[0]))
        writer.writeheader()
        writer.writerows(filtered)
        st.download_button(
            "조회 결과 CSV 다운로드",
            data=output.getvalue().encode("utf-8-sig"),
            file_name="paper-trades.csv",
            mime="text/csv",
            on_click="ignore",
        )
    else:
        st.info("선택한 조건에 해당하는 체결이 없습니다.")


def select_account():
    default_path = base_account_path()
    if not st.session_state.get("unused-market-seeds-cleared"):
        try:
            clear_unused_market_seeds(default_path.parent)
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            st.warning("기존 종목 계좌의 초기 자금을 정리하지 못했습니다.")
        else:
            st.session_state["unused-market-seeds-cleared"] = True
    next_db = st.session_state.pop("next-paper-db", None)
    if next_db is not None:
        st.session_state["paper-db"] = next_db
    next_market = st.session_state.pop("next-market", None)
    if next_market is not None:
        try:
            current_path = Path(st.session_state.get("paper-db", default_path))
            current, _, _ = read_account(current_path)
            current_market = json.loads(current["settings"])["market"]
            per_market = st.session_state.setdefault("paper-accounts-by-market", {})
            per_market[current_market] = str(current_path)
            if next_market not in per_market:
                per_market[next_market] = str(market_account_path(next_market))
            st.session_state["paper-db"] = per_market[next_market]
        except (sqlite3.Error, ValueError, TypeError) as error:
            st.session_state["market-error"] = f"모의계좌를 열 수 없습니다 · {error}"
    path = Path(st.session_state.get("paper-db", default_path))
    if path.parent != default_path.parent or not path.is_file():
        st.session_state.pop("paper-db", None)
        return default_path
    return path


if __name__ == "__main__":
    st.set_page_config(page_title="비트코인 모의매매 AI 에이전트", layout="wide")
    title, theme = st.columns([4, 1], vertical_alignment="center")
    with title:
        st.title("비트코인 모의매매 AI 에이전트", anchor=False)
        st.caption("로그인 계정별 가상계좌")
    with theme:
        render_theme_picker(key="dashboard-theme")
    config = load_config()
    login = None
    if config is not None:
        try:
            login = current_login(config)
        except SupabaseError:
            st.error("로그인 서버에 연결하지 못했습니다. 잠시 후 새로고침해 주세요.")
            st.stop()
    notice = st.session_state.pop("auth-notice", None)
    if notice:
        st.success(notice)
    page = st.session_state.get("auth-page", "market")
    if login is None:
        if page in ("login", "signup", "recover"):
            show_auth_page(config, page)
            st.stop()
        if st.sidebar.button("로그인 / 회원가입"):
            navigate("login")
        path = select_account()
    else:
        show_account_controls(config, login)
        if page == "account" or login["must_change_password"]:
            show_account_settings(config, login)
            st.stop()
        with show_storage_error(), account_changes(config):
            path = select_account()
    st.html("""
    <style>
    .st-key-market-workspace { max-width: 1380px; margin-inline: auto; }
    .st-key-market-chart-host { min-width: 0; max-width: 100%; overflow-x: auto; }
    </style>
    """)
    try:
        active_account, _, _ = read_account(path)
        if active_account is not None:
            st.session_state["active-market"] = json.loads(active_account["settings"])["market"]
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        pass
    with st.sidebar:
        show_krw_market()
    show_account(path, config, guest=login is None)
    if login is None and st.session_state.pop("show-login-dialog", False):
        login_dialog()
