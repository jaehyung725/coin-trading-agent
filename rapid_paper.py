"""Fast simulated directional execution using completed hourly and minute bars."""

from datetime import timedelta
from math import nextafter
from types import SimpleNamespace
from urllib.error import URLError

from agent import contiguous, history_size, recent_candles, record_hourly_return, save_auto_decision
from leveraged_paper import leverage_in_transaction
from llm_strategy import ANALYSIS_INTERVAL, LLM_STRATEGY, chart_decision
from operation_logs import operation_logger
from strategies import ENTRY_PERCENTAGES, LEVERAGE_LEVELS, STRATEGIES, indicator_series


DEFAULT_LEVERAGE = LEVERAGE_LEVELS["Light"]
LEGACY_ENTRY_PERCENT = 5
STOP = 0.005
TARGET = 0.01
MARKET_ERRORS = (URLError, TimeoutError, ValueError, KeyError, TypeError)


def hourly_bias(history, settings):
    strategy = settings["strategy"]
    label = STRATEGIES[strategy]
    series = indicator_series(history, (label,), settings)
    last = history[-1].closing
    if strategy == "bollinger":
        difference = last - series["밴드 중심선"].iloc[-1]
    elif strategy == "rsi14":
        difference = series[label].iloc[-1] - 50
    elif strategy == "macd":
        difference = series["히스토그램"].iloc[-1]
    else:
        difference = last - series[label].iloc[-1]
    return "long" if difference > 0 else "short" if difference < 0 else None


def rapid_market(settings, now, loader):
    hour = now.replace(minute=0, second=0, microsecond=0)
    minute = now.replace(second=0, microsecond=0)
    hourly_args = SimpleNamespace(**{"ml_threshold": None, **settings, "minutes": 60})
    window = history_size(hourly_args)
    hourly = loader(hourly_args, hour, count=window)
    minutes = loader(SimpleNamespace(market=settings["market"], minutes=1), minute + timedelta(minutes=1), count=4)
    if (len(hourly) < window or hourly[-1].start != hour - timedelta(hours=1) or not contiguous(hourly, 60)
            or len(minutes) < 4 or minutes[-1].start != minute or not contiguous(minutes, 1)):
        raise ValueError("Incomplete market history")
    completed = minutes[-4:-1]
    change = completed[-1].closing - completed[0].closing
    last_change = completed[-1].closing - completed[-2].closing
    momentum = "long" if change > 0 and last_change > 0 else "short" if change < 0 and last_change < 0 else None
    bias = hourly_bias(hourly, settings)
    return minute, minutes[-1].closing, bias, momentum


def prepare_rapid_progress(db):
    db.execute("""CREATE TABLE IF NOT EXISTS rapid_progress (
        id INTEGER PRIMARY KEY CHECK(id=1), minute TEXT NOT NULL)""")


def activate_rapid_strategy(db, now):
    prepare_rapid_progress(db)
    minute = now.replace(second=0, microsecond=0).isoformat()
    db.execute("""INSERT INTO rapid_progress VALUES (1, ?)
        ON CONFLICT(id) DO UPDATE SET minute=MAX(rapid_progress.minute, excluded.minute)""", (minute,))
    save_auto_decision(db, "활성화", "자동매매를 시작했습니다. 시작 이후 처음 마감되는 1분봉부터 신호를 확인합니다.")


def rapid_tick(db, settings, *, now, candle_loader=recent_candles):
    minute = now.replace(second=0, microsecond=0)
    key = minute.isoformat()
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='rapid_progress'").fetchone()
    if exists:
        last = db.execute("SELECT minute FROM rapid_progress WHERE id=1").fetchone()
        if last and last[0] >= key:
            return
    llm = settings["strategy"] == LLM_STRATEGY

    def load_candles(market, interval, end, count):
        return candle_loader(SimpleNamespace(market=market, minutes=interval), end, count=count)

    try:
        if llm:
            current = load_candles(settings["market"], 1, minute + timedelta(minutes=1), 1)
            if not current or current[-1].start != minute:
                raise ValueError("Current market price unavailable")
            mark = current[-1].closing
            direction = None
        else:
            _, mark, bias, momentum = rapid_market(settings, now, candle_loader)
            direction = bias if bias == momentum else None
    except MARKET_ERRORS as error:
        operation_logger("worker").warning("Automatic market data unavailable market=%s type=%s",
                                           settings["market"], type(error).__name__)
        with db:
            save_auto_decision(db, "SKIP", "최근 마감 1시간봉·1분봉 또는 현재 시세를 확인하지 못했습니다.")
        return

    with db:
        db.execute("BEGIN IMMEDIATE")
        prepare_rapid_progress(db)
        last = db.execute("SELECT minute FROM rapid_progress WHERE id=1").fetchone()
        if last and last[0] >= key:
            return
        cash, position, _, _, totals = leverage_in_transaction(db, source="auto")
        order = None
        event = None
        risk_exit = None
        if position is not None:
            move = position.direction * (mark / position.entry - 1)
            if position.is_liquidated(mark):
                risk_exit = "모의 청산가 도달"
            elif move <= -STOP:
                risk_exit = "가격 기준 손절 도달"
            elif move >= TARGET:
                risk_exit = "가격 기준 익절 도달"

        reason = "1시간봉 방향과 최근 3개 마감 1분봉 움직임이 일치하지 않아 대기합니다."
        model_exit = False
        leverage = settings.get("rapid_leverage", DEFAULT_LEVERAGE)
        entry_percent = settings.get("rapid_entry_percent", LEGACY_ENTRY_PERCENT)
        if llm and risk_exit is None:
            reason = "다음 5분 분석 시점을 기다립니다."
            if minute.minute % ANALYSIS_INTERVAL == 0:
                decision = chart_decision(settings, minute, mark, position, load_candles)
                reason = decision.reason
                if position is None and decision.action in ("long", "short"):
                    direction = decision.action
                    leverage, entry_percent = decision.leverage, decision.entry_percent
                elif position is not None and decision.action == "close":
                    model_exit = True

        if position is not None:
            opposite = not llm and direction is not None and direction != position.side
            if risk_exit or model_exit or opposite:
                reason = risk_exit or ("반대 방향 신호가 확인되었습니다." if opposite else reason)
                order = {"quantity": position.quantity, "price": position.exit_price(mark),
                         "fee": 0.0 if position.is_liquidated(mark) else position.quantity * position.exit_price(mark) * position.fee}
                cash, position, _, event, totals = leverage_in_transaction(
                    db, mark=mark, close_side=position.side, source="auto")
            elif not llm:
                reason = "열린 포지션을 유지합니다."
        elif direction is not None and cash > 0:
            if leverage not in LEVERAGE_LEVELS.values():
                raise ValueError("Unsupported automatic trading leverage")
            if entry_percent not in (*ENTRY_PERCENTAGES, LEGACY_ENTRY_PERCENT):
                raise ValueError("Unsupported automatic trading allocation")
            fee = settings["fee"]
            budget = cash * (entry_percent / 100)
            margin = budget / (1 + leverage * fee)
            while margin + margin * leverage * fee > budget:
                margin = nextafter(margin, 0.0)
            cash, position, _, event, totals = leverage_in_transaction(
                db, mark=mark, order=(direction, margin, leverage, fee, settings["slippage"]), source="auto")
            order = {"quantity": position.quantity, "price": position.entry, "fee": position.entry_fee}
            if llm:
                reason = f"레버리지 {leverage}배 · 가용 현금 {entry_percent}% · {reason}"
            else:
                reason = "1시간봉 지표 방향과 최근 3개 마감 1분봉의 움직임이 일치했습니다."
        save_auto_decision(db, event or "HOLD", reason, candle=(minute - timedelta(minutes=1)).isoformat(),
                           mark=mark, order=order)
        db.execute("INSERT OR REPLACE INTO rapid_progress VALUES (1, ?)", (key,))
        units, = db.execute("SELECT units FROM account WHERE id=1").fetchone()
        equity = cash + units * mark + (position.release(mark) if position else 0.0)
        funding = db.execute("SELECT 1 FROM sqlite_master WHERE name='cash_adjustments'").fetchone()
        net = db.execute("SELECT COALESCE(SUM(amount), 0) FROM cash_adjustments").fetchone()[0] if funding else 0.0
        record_hourly_return(db, 100 * (equity - net - settings["cash"]) / settings["cash"], minute)
