"""Local dashboard for the paper trading agent."""

import csv
import io
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from html import escape
from tempfile import TemporaryDirectory, mkstemp
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError
from urllib.request import urlopen

import altair as alt
import pandas as pd
import streamlit as st

from market_grid import render_market_grid
from theme_picker import render_theme_picker

from agent import (Account, adjust_paper_cash,
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


def show_trades(rows):
    st.dataframe(
        rows, hide_index=True,
        column_config={
            "체결가": st.column_config.NumberColumn(format="localized"),
            "수량": st.column_config.NumberColumn(format="%.8f"),
            "수수료": st.column_config.NumberColumn(format="localized"),
            "체결 후 평균 매입가": st.column_config.NumberColumn(format="localized"),
            "실현 손익": st.column_config.NumberColumn(format="localized"),
            "체결 후 현금": st.column_config.NumberColumn(format="localized"),
        },
    )


def kst_axis(title, date_only=False):
    label_format = "%y-%m-%d" if date_only else "%m-%d %H:%M"
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


def show_price_chart(candles, fills, settings, quote, boundary, now, count, indicators, average, date_only=False):
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

    series = indicator_series(candles, indicators, settings)
    first = max(0, len(candles) - count)
    points = []
    for index in range(first, len(candles)):
        candle = candles[index]
        point = {
            "시각": candle.start.isoformat(),
            "한국 시간": candle.start.astimezone(KST).strftime("%Y-%m-%d %H:%M"),
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
                "한국 시간": time.astimezone(KST).strftime("%Y-%m-%d %H:%M"),
                "구분": SIDE_LABELS[fill["side"]],
                "체결가": fill["price"],
            })

    x = kst_axis("봉 시작 시각 (KST)", date_only)
    price_scale = alt.Scale(zero=False)
    source = alt.Chart(alt.Data(values=points)).encode(
        x=x,
        tooltip=["한국 시간:N", "상태:N", *[
            alt.Tooltip(f"{field}:Q", format=",.2f") for field in ("시가", "고가", "저가", "종가", "거래량")
        ]],
    )
    direction = alt.condition(
        alt.datum["종가"] >= alt.datum["시가"], alt.value("#0284c7"), alt.value("#cd425e")
    )
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
            color=alt.condition(alt.datum["히스토그램"] >= 0, alt.value("#168176"), alt.value("#cd425e")),
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
    st.caption(f"최근 봉 {len(points)}개 · 진행 중인 봉은 5초마다 조회 · 삼각형은 모의 체결가")


def performance_points(candles, fills, initial_cash, minutes, adjustments=()):
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
                cash, units = fill["cash"], fill["units"]
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
                "누적 수익률": 100 * ((baseline_equity - net_funding) / initial_cash - 1),
                "모의 전략": 0.0,
                "BTC 보유": 0.0,
            })

        while fill is not None and datetime.fromisoformat(fill["time"]) < close_time:
            if "amount" in fill.keys():
                cash += fill["amount"]
                net_funding += fill["amount"]
            else:
                cash, units = fill["cash"], fill["units"]
            fill = next(trades, None)

        equity = cash + units * candle.closing
        adjusted = equity - (net_funding - starting_funding)
        peak = max(peak, adjusted)
        if peak > 0:
            drawdown = max(drawdown, 100 * (1 - adjusted / peak))
        points.append({
            "시각": close_time.isoformat(),
            "한국 시간": close_time.astimezone(KST).strftime("%Y-%m-%d %H:%M"),
            "누적 수익률": 100 * ((equity - net_funding) / initial_cash - 1),
            "모의 전략": 100 * (adjusted / baseline_equity - 1) if baseline_equity > 0 else None,
            "BTC 보유": 100 * (candle.closing / baseline_price - 1),
        })
    return (points, drawdown) if points else ([], None)


def show_performance(candles, fills, rows, settings, quote, adjustments):
    st.subheader("모의매매 성과")
    profits = [row["실현 손익"] for row in rows if row["실현 손익"] is not None]
    completed = len(profits)
    average = f"{sum(profits) / completed:+,.2f} {quote}" if completed else "—"
    win_rate = f"{sum(profit > 0 for profit in profits) / completed:.1%}" if completed else "—"
    total, mean_profit, win = st.columns(3)
    total.metric("누적 실현 손익", f"{sum(profits):+,.2f} {quote}")
    mean_profit.metric("매도 체결당 평균 손익", average)
    win.metric(f"승률 · 매도 {completed:,}건", win_rate)

    points, drawdown = performance_points(candles, fills, settings["cash"], settings["minutes"], adjustments)
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
        step, precision = {"quantity": (0.0001, "%.8f"), "cash": (1000.0, "%.2f"),
                           "percent": (5.0, "%.2f")}[unit]
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


@st.cache_data(ttl=15, show_spinner=False)
def load_krw_tickers():
    with urlopen("https://api.upbit.com/v1/ticker/all?quote_currencies=KRW", timeout=5) as response:
        return [row for row in json.load(response) if row["market"].startswith("KRW-")]


def base_account_path():
    if "demo-directory" not in st.session_state:
        directory = TemporaryDirectory(prefix="bollinger-demo-")
        path = Path(directory.name) / "paper.sqlite3"
        settings = dict(market="KRW-BTC", minutes=15, period=20, deviations=2,
                        cash=1_000_000, allocation=0.1, fee=0.0005, slippage=0.0005)
        with closing(sqlite3.connect(path)) as db, db:
            initialize_account(db, settings, settings["cash"])
        st.session_state["demo-directory"] = directory
    return Path(st.session_state["demo-directory"].name) / "paper.sqlite3"


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
            initialize_account(db, {**settings, "market": market}, settings["cash"])
    return path


@st.fragment(run_every="15s")
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
        st.info("원화 마켓 시세를 불러오지 못했습니다. 15초 후 다시 조회합니다.")
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
        change = 100 * ticker["signed_change_rate"]
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


def show_order_and_account(path, account, settings, args, live, quote, asset, orders):
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
        st.caption(f"현재 가용 현금 {account['cash']:,.2f} {quote}")
        target = st.number_input(f"변경할 가용 현금 ({quote})", min_value=0.0, value=None,
                                 key=f"{path}:cash-target")
        if st.button("가용 현금 변경", disabled=target is None, key=f"{path}:apply-cash"):
            try:
                adjust_paper_cash(path, target)
            except (sqlite3.Error, ValueError) as error:
                st.error(str(error))
            else:
                st.session_state[f"{path}:cash-changed"] = True
                st.rerun(scope="app")
        if st.session_state.pop(f"{path}:cash-changed", False):
            st.success("가용 현금을 변경했습니다.")

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
        starting_cash = st.number_input(f"새 계좌 초기 자금 ({quote})", min_value=0.01,
                                        value=float(settings["cash"]), key=f"{path}:new-cash")
        if st.button("새 모의계좌 시작", key=f"{path}:new-account"):
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


@st.fragment(run_every="5s")
def show_account(path):
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
        st.caption(f"{status}마지막 처리 봉 {processed:%Y-%m-%d %H:%M} KST · 시세 5초마다 조회")
    else:
        st.caption("처리된 봉 없음 · 시세 5초마다 조회")

    buys = sum(fill["side"] == "BUY" for fill in fills)
    cash, position, count, fees = st.columns(4)
    cash.metric("가용 현금", f"{account['cash']:,.2f}", help=quote, border=True)
    position.metric("보유 수량", f"{account['units']:,.8f}", help=asset, border=True)
    count.metric("누적 체결", f"{account['trades']:,}건",
                 help=f"매수 {buys:,}건 · 매도 {len(fills) - buys:,}건", border=True)
    fees.metric("누적 추정 수수료", f"{sum(row['수수료'] for row in rows):,.2f}",
                help=quote, border=True)
    st.caption(f"금액 {quote} · 보유 수량 {asset} · 매수 {buys:,}건 · 매도 {len(fills) - buys:,}건")

    args = SimpleNamespace(**{"ml_threshold": None, **settings})
    try:
        now, boundary, market_candles = load_account_market(settings)
    except (URLError, TimeoutError, ValueError, KeyError, TypeError):
        now = datetime.now(timezone.utc)
        interval = args.minutes * 60
        seconds = int(now.timestamp())
        boundary = datetime.fromtimestamp(seconds - seconds % interval, timezone.utc)
        market_candles = []
        st.warning("업비트 공개 시세를 불러오지 못했습니다. 5초 후 다시 조회합니다.")

    seconds = int(now.timestamp())
    candles = [candle for candle in market_candles if candle.start < boundary]
    latest = market_candles[-1] if market_candles else None
    live = latest if latest is not None and latest.start == boundary else None
    try:
        orders = paper_limit_orders(path, market_price=live.closing if live else None)
        current, _, _ = read_account(path)
        if current["trades"] != account["trades"]:
            st.rerun(scope="app")
    except sqlite3.Error as error:
        orders = []
        st.warning(f"지정가 주문을 확인할 수 없습니다 · {error}")
    average = rows[0]["체결 후 평균 매입가"] if rows else None
    cost = (average or 0) * account["units"]
    net_funding = sum(event["amount"] for event in adjustments)

    equity, performance, open_profit, avg_entry = st.columns(4)
    avg_entry.metric("보유 평균 매입가", f"{average:,.2f} {quote}" if average is not None else "—",
                     border=True)
    if latest is not None or account["units"] == 0:
        mark = latest.closing if latest is not None else 0
        summary = Account(account["cash"], account["units"]).summary(mark, settings["cash"])
        equity.metric("평가자산", f"{summary['equity']:,.2f} {quote}", border=True)
        return_pct = 100 * ((account["cash"] + account["units"] * mark - net_funding) / settings["cash"] - 1)
        performance.metric("현금 조정 제외 수익률", f"{return_pct:+.4f}%", border=True)
        unrealized = account["units"] * mark - cost if account["units"] else 0.0
        open_profit.metric("보유 평가손익", f"{unrealized:+,.2f} {quote}", border=True)
        if latest is not None:
            st.caption(
                f"평가 기준 {latest.closing:,.2f} {quote} · "
                f"{latest.start.astimezone(KST):%Y-%m-%d %H:%M} KST 시작 봉"
            )
    else:
        equity.metric("평가자산", "시세 필요", border=True)
        performance.metric("현금 조정 제외 수익률", "시세 필요", border=True)
        open_profit.metric("보유 평가손익", "시세 필요", border=True)

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
                pnl_col.metric("예상 순손익", f"{proceeds - cost:+,.2f} {quote}")

    with st.container(key="market-workspace"):
        chart_panel, order_panel = st.columns([3, 1], gap="medium")
    with chart_panel.container(border=True, width="stretch", key="market-chart-host"):
        interval_col, count_col, overlay_col = st.columns(3)
        intervals = (1, 3, 5, 10, 15, 30, 60, 240, 1440, 10080)
        chart_minutes = interval_col.selectbox("차트 간격", intervals,
                                               index=intervals.index(args.minutes),
                                               format_func=lambda value: {1440: "일봉", 10080: "주봉"}.get(value, f"{value}분"),
                                               key=f"{path}:chart-interval")
        count = count_col.selectbox("표시 봉 수", (30, 60, 100, 200), index=2,
                                    key=f"{path}:chart-count")
        indicators = overlay_col.multiselect("차트 지표", INDICATORS, default=[],
                                              key=f"{path}:chart-indicators")
        if chart_minutes == 10080:
            chart_boundary = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())
        else:
            interval = chart_minutes * 60
            chart_boundary = datetime.fromtimestamp(seconds - seconds % interval, timezone.utc)
        chart_candles = market_candles
        if chart_minutes != args.minutes:
            try:
                chart_candles = load_market_candles(market, chart_minutes, chart_boundary)
            except (URLError, TimeoutError, ValueError, KeyError, TypeError):
                chart_candles = []
        show_price_chart(chart_candles, fills, settings, quote, chart_boundary, now, count, indicators, average,
                         date_only=chart_minutes >= 1440)
        base, symbol = market.split("-", maxsplit=1)
        st.link_button("업비트 원본 차트 열기", f"https://upbit.com/exchange?code=CRIX.UPBIT.{base}-{symbol}")

    with order_panel:
        show_order_and_account(path, account, settings, args, live, quote, asset, orders)

    chart, strategy = st.columns([2, 1])
    with chart.container(border=True):
        show_performance(candles, fills, rows, settings, quote, adjustments)
    with strategy.container(border=True):
        show_strategy(settings, quote)

    if adjustments:
        with st.expander(f"가용 현금 변경 내역 · 순조정 {net_funding:+,.2f} {quote}"):
            st.dataframe([{
                "변경 시각 (KST)": local_time(event["time"]).strftime("%Y-%m-%d %H:%M:%S"),
                f"변경액 ({quote})": event["amount"],
            } for event in reversed(adjustments)], hide_index=True)

    st.subheader("체결 내역")
    if not rows:
        st.info("아직 체결 내역이 없습니다. 모의매매 조건이 충족되면 여기에 표시됩니다.")
        return

    recent, history = st.tabs(["최근 체결", "전체 이력"])
    with recent:
        st.caption(f"마지막 체결 {rows[0]['체결 시각 (KST)']} KST · 금액 단위 {quote}")
        show_trades(rows[:5])
    with history:
        show_history(rows, path)


def show_history(rows, path):
    side_col, start_col, end_col = st.columns(3)
    side = side_col.selectbox("매매 구분", ("전체", *SIDE_LABELS.values()), key=f"{path}:side")
    start = start_col.date_input("시작일", value=None, key=f"{path}:start")
    end = end_col.date_input("종료일", value=None, key=f"{path}:end")
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


if __name__ == "__main__":
    st.set_page_config(page_title="비트코인 모의매매 AI 에이전트", layout="wide")
    title, theme = st.columns([4, 1], vertical_alignment="center")
    with title:
        st.title("비트코인 모의매매 AI 에이전트", anchor=False)
        st.caption("공개 데모 · 방문자별 임시 가상계좌")
    with theme:
        render_theme_picker(key="dashboard-theme")
    default_path = base_account_path()
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
    show_account(path)
