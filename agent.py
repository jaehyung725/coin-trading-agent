"""Bollinger Band spot paper trading and backtesting."""
import argparse
import csv
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
from statistics import mean, pstdev
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen


@dataclass(frozen=True)
class Candle:
    start: datetime
    opening: float
    closing: float
    high: float | None = None
    low: float | None = None
    volume: float | None = None

    def __post_init__(self):
        if self.start.utcoffset() is None:
            raise ValueError("Candle timestamps need a timezone")
        if any(not math.isfinite(price) or price <= 0 for price in (self.opening, self.closing)):
            raise ValueError("Candle prices must be finite and positive")


def bollinger_bands(closes, deviations):
    middle = mean(closes)
    offset = deviations * pstdev(closes)
    return middle - offset, middle, middle + offset


def signal(history, holding, period, deviations):
    if len(history) < period + 1:
        return None

    previous = [c.closing for c in history[-period - 1:-1]]
    current = [c.closing for c in history[-period:]]
    lower, middle, _ = bollinger_bands(current, deviations)
    if holding:
        return "SELL" if current[-1] >= middle else None

    previous_lower, _, _ = bollinger_bands(previous, deviations)
    if previous[-1] < previous_lower and lower < current[-1] < middle:
        return "BUY"
    return None


def entry_features(candles):
    closes = [c.closing for c in candles]
    middle = mean(closes)
    spread = pstdev(closes)
    distance = (closes[-1] - middle) / spread if spread else 0.0
    change = 100 * (closes[-1] / closes[-2] - 1)
    return (1.0, max(-5.0, min(5.0, distance)), max(-5.0, min(5.0, change)))


def entry_score(history, args):
    """Fit a small logistic model using only outcomes already observed."""
    weights = [0.0, 0.0, 0.0]

    def predict(features):
        logit = sum(weight * value for weight, value in zip(weights, features))
        return 1 / (1 + math.exp(-max(-30, min(30, logit))))

    for index in range(args.period, len(history)):
        features = entry_features(history[index - args.period:index])
        outcome = history[index]
        buy_cost = outcome.opening * (1 + args.slippage) * (1 + args.fee)
        sell_value = outcome.closing * (1 - args.slippage) * (1 - args.fee)
        error = int(sell_value > buy_cost) - predict(features)
        for column, value in enumerate(features):
            weights[column] += 0.05 * error * value

    return predict(entry_features(history[-args.period:]))


def history_size(args):
    return 199 if args.ml_threshold is not None else args.period + 1


def decision(history, account, args):
    side = signal(history, account.units > 0, args.period, args.deviations)
    if side == "BUY" and args.ml_threshold is not None:
        score = entry_score(history, args)
        return (side if score >= args.ml_threshold else None), {"entry_score": score}
    return side, {}


@dataclass
class Account:
    cash: float
    units: float = 0.0
    trades: int = 0

    def order_quote(self, side, reference_price, args, *, size=None, unit="quantity"):
        direction = 1 if side == "BUY" else -1
        price = reference_price * (1 + direction * args.slippage)
        buy_cost = price * (1 + args.fee)
        limit = self.cash / buy_cost if side == "BUY" else self.units

        if size is None:
            quantity = limit * (args.allocation if side == "BUY" else 1)
        elif unit == "percent":
            if size > 100:
                raise ValueError("비율은 100% 이하여야 합니다.")
            quantity = limit * size / 100
        elif unit == "cash" and side == "BUY":
            quantity = size / buy_cost
        else:
            quantity = size

        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("주문 수량은 0보다 커야 합니다.")
        if quantity > limit and not math.isclose(quantity, limit, rel_tol=1e-12):
            raise ValueError("가용 현금 또는 보유 수량이 부족합니다.")
        quantity = min(quantity, limit)
        gross = quantity * price
        return quantity, price, gross, gross * args.fee

    def fill(self, side, opening, args, *, size=None, unit="quantity"):
        quantity, price, gross, fee = self.order_quote(side, opening, args, size=size, unit=unit)
        if side == "BUY":
            self.cash = max(0.0, self.cash - (gross + fee))
            self.units += quantity
        else:
            self.cash += gross - fee
            self.units -= quantity
        self.trades += 1
        return price

    def summary(self, mark, initial):
        equity = self.cash + self.units * mark
        return dict(cash=round(self.cash, 2), units=self.units, fills=self.trades,
                    equity=round(equity, 2), return_pct=round(100 * (equity / initial - 1), 4))


def read_csv(path):
    with path.open(newline="", encoding="utf-8-sig") as source:
        candles = [
            Candle(datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")),
                   float(row["open"]), float(row["close"]))
            for row in csv.DictReader(source)
        ]
    if any(left.start >= right.start for left, right in zip(candles, candles[1:])):
        raise ValueError("CSV timestamps must be strictly increasing")
    return candles


def demo():
    prices = [100] * 20 + [80, 98, 100, 102, 101]
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return [Candle(start + timedelta(minutes=15 * i), price, price)
            for i, price in enumerate(prices)]


def backtest(candles, args):
    window = history_size(args)
    if len(candles) < window + 1:
        raise ValueError(f"Need at least {window + 1} candles")
    account = Account(args.cash)
    for index in range(window, len(candles)):
        history = candles[index - window:index]
        side, details = decision(history, account, args)
        if side:
            candle = candles[index]
            price = account.fill(side, candle.opening, args)
            print(json.dumps(dict(time=candle.start.isoformat(), side=side, price=price, **details)))
        elif details:
            print(json.dumps(dict(time=candles[index].start.isoformat(), side="HOLD", **details)))
    print(json.dumps(account.summary(candles[-1].closing, args.cash)))


def recent_candles(args, before, *, count=None):
    if count is None:
        count = history_size(args) + 1
    query = urlencode(dict(market=args.market, count=count, to=before.isoformat()))
    interval = {1440: "days", 10080: "weeks"}.get(args.minutes, f"minutes/{args.minutes}")
    url = f"https://api.upbit.com/v1/candles/{interval}?{query}"
    with urlopen(url, timeout=10) as response:
        rows = json.load(response)
    return sorted(
        (Candle(datetime.fromisoformat(row["candle_date_time_utc"]).replace(tzinfo=timezone.utc),
                float(row["opening_price"]), float(row["trade_price"]),
                float(row["high_price"]), float(row["low_price"]),
                float(row["candle_acc_trade_volume"])) for row in rows),
        key=lambda candle: candle.start,
    )


def record_fill(db, account, side, reference_price, args, timestamp, *, size=None, unit="quantity"):
    price = account.fill(side, reference_price, args, size=size, unit=unit)
    db.execute("INSERT INTO fills VALUES (?, ?, ?, ?, ?)",
               (timestamp, side, price, account.cash, account.units))
    return price


def manual_fill(path, side, reference_price, *, size, unit):
    if side not in ("BUY", "SELL") or not math.isfinite(reference_price) or reference_price <= 0:
        raise ValueError("올바른 매매 구분과 시세가 필요합니다.")

    with closing(sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        settings, cash, units, trades = db.execute(
            "SELECT settings, cash, units, trades FROM account WHERE id = 1"
        ).fetchone()
        args = argparse.Namespace(**json.loads(settings))
        account = Account(cash, units, trades)
        timestamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        price = record_fill(db, account, side, reference_price, args, timestamp, size=size, unit=unit)
        db.execute("UPDATE account SET cash=?, units=?, trades=? WHERE id=1",
                   (account.cash, account.units, account.trades))
        return price


def paper_limit_orders(path, *, order=None, cancel=None, market_price=None):
    """Place, cancel, or process simulated limit orders in a single transaction."""
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True)) as db, db:
        db.row_factory = sqlite3.Row
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='limit_orders'"
        ).fetchone()
        if not exists and order is None:
            return []
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS limit_orders (
            id INTEGER PRIMARY KEY, side TEXT NOT NULL, price REAL NOT NULL,
            quantity REAL NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN')""")
        row = db.execute("SELECT settings, cash, units, trades FROM account WHERE id=1").fetchone()
        args = argparse.Namespace(**json.loads(row["settings"]))
        args.slippage = 0.0
        account = Account(row["cash"], row["units"], row["trades"])
        if order is not None:
            side, price, size, unit = order
            if side not in ("BUY", "SELL") or not math.isfinite(price) or price <= 0:
                raise ValueError("지정가는 0보다 커야 합니다.")
            quantity, _, _, _ = account.order_quote(side, price, args, size=size, unit=unit)
            db.execute("INSERT INTO limit_orders (side, price, quantity) VALUES (?, ?, ?)",
                       (side, price, quantity))
        if cancel is not None:
            db.execute("UPDATE limit_orders SET status='CANCELED' WHERE id=? AND status='OPEN'", (cancel,))
        if market_price is not None and math.isfinite(market_price) and market_price > 0:
            matched = db.execute("""SELECT * FROM limit_orders WHERE status='OPEN'
                AND ((side='BUY' AND price>=?) OR (side='SELL' AND price<=?)) ORDER BY id""",
                (market_price, market_price)).fetchall()
            for item in matched:
                timestamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
                try:
                    record_fill(db, account, item["side"], market_price, args, timestamp,
                                size=item["quantity"])
                except ValueError:
                    status = "REJECTED"
                else:
                    status = "FILLED"
                db.execute("UPDATE limit_orders SET status=? WHERE id=?", (status, item["id"]))
            if matched:
                db.execute("UPDATE account SET cash=?, units=?, trades=? WHERE id=1",
                           (account.cash, account.units, account.trades))
        return db.execute("""SELECT * FROM limit_orders
            ORDER BY (status='OPEN') DESC, id DESC LIMIT 100""").fetchall()


def paper_tick(db, args):
    # Capture the clock before fetching so an interval boundary cannot move mid-request.
    now = int(time.time())
    interval = args.minutes * 60
    start = datetime.fromtimestamp(now - now % interval, timezone.utc)
    candles = recent_candles(args, start + timedelta(seconds=interval))
    if len(candles) < history_size(args) + 1 or candles[-1].start != start:
        return
    if candles[-2].start != start - timedelta(seconds=interval):
        return

    key = start.isoformat()
    with db:
        db.execute("BEGIN IMMEDIATE")
        last, cash, units, trades = db.execute(
            "SELECT last, cash, units, trades FROM account"
        ).fetchone()
        if last >= key:
            return
        account = Account(cash, units, trades)
        recent_fill = db.execute("SELECT time FROM fills ORDER BY time DESC LIMIT 1").fetchone()
        has_adjustments = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='cash_adjustments' AND type='table'"
        ).fetchone()
        recent_adjustment = db.execute(
            "SELECT time FROM cash_adjustments ORDER BY time DESC LIMIT 1"
        ).fetchone() if has_adjustments else None
        if any(record and datetime.fromisoformat(record[0]) >= start
               for record in (recent_fill, recent_adjustment)):
            # Avoid recording an opening fill before a later account change.
            side, details = None, {}
        else:
            side, details = decision(candles[:-1], account, args)
        if side == "BUY" and account.cash <= 0:
            side = None
        if side:
            record_fill(db, account, side, candles[-1].opening, args, key)
        db.execute("UPDATE account SET last=?, cash=?, units=?, trades=?",
                   (key, account.cash, account.units, account.trades))
        net_funding = db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM cash_adjustments"
        ).fetchone()[0] if has_adjustments else 0.0
    summary = account.summary(candles[-1].closing, args.cash)
    if net_funding:
        equity = account.cash + account.units * candles[-1].closing
        summary["return_pct"] = round(100 * ((equity - net_funding) / args.cash - 1), 4)
    print(json.dumps(dict(time=key, side=side or "HOLD", **details, **summary)), flush=True)


def initialize_account(db, settings, cash):
    db.execute("""CREATE TABLE IF NOT EXISTS account (
        id INTEGER PRIMARY KEY CHECK(id=1), settings TEXT, last TEXT,
        cash REAL, units REAL, trades INTEGER)""")
    db.execute("""CREATE TABLE IF NOT EXISTS fills (
        time TEXT PRIMARY KEY, side TEXT, price REAL, cash REAL, units REAL)""")
    db.execute("INSERT OR IGNORE INTO account VALUES (1, ?, '', ?, 0, 0)",
               (json.dumps(settings, sort_keys=True), cash))


def adjust_paper_cash(path, target):
    if not math.isfinite(target) or target < 0:
        raise ValueError("가용 현금은 0 이상이어야 합니다.")
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        cash, = db.execute("SELECT cash FROM account WHERE id=1").fetchone()
        change = target - cash
        if change == 0:
            return
        db.execute("""CREATE TABLE IF NOT EXISTS cash_adjustments (
            time TEXT PRIMARY KEY, amount REAL NOT NULL)""")
        timestamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        db.execute("INSERT INTO cash_adjustments VALUES (?, ?)", (timestamp, change))
        db.execute("UPDATE account SET cash=? WHERE id=1", (target,))


def paper(args):
    settings = {name: getattr(args, name) for name in (
        "market", "minutes", "period", "deviations", "cash", "allocation", "fee", "slippage"
    )}
    if args.ml_threshold is not None:
        settings["ml_threshold"] = args.ml_threshold
    db = sqlite3.connect(args.db)
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            initialize_account(db, settings, args.cash)
            if json.loads(db.execute("SELECT settings FROM account").fetchone()[0]) != settings:
                raise ValueError("Strategy settings differ from this account. Use a different --db.")
        while True:
            try:
                paper_tick(db, args)
            except (URLError, TimeoutError, ValueError, KeyError, TypeError) as error:
                if args.once:
                    raise
                print(f"Market data unavailable: {error}", flush=True)
            if args.once:
                break
            time.sleep(args.poll)
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    historical = modes.add_parser("backtest")
    data = historical.add_mutually_exclusive_group(required=True)
    data.add_argument("--csv", type=Path)
    data.add_argument("--demo", action="store_true")
    polling = modes.add_parser("paper")
    polling.add_argument("--market", default="KRW-BTC")
    polling.add_argument("--minutes", type=int, default=15, choices=[1, 3, 5, 10, 15, 30, 60, 240])
    polling.add_argument("--db", type=Path, default=Path("paper.sqlite3"))
    polling.add_argument("--poll", type=int, default=30)
    polling.add_argument("--once", action="store_true")
    for mode in (historical, polling):
        mode.add_argument("--period", type=int, default=20)
        mode.add_argument("--deviations", type=float, default=2)
        mode.add_argument("--cash", type=float, default=1_000_000)
        mode.add_argument("--allocation", type=float, default=0.1)
        mode.add_argument("--fee", type=float, default=0.0005)
        mode.add_argument("--slippage", type=float, default=0.0005)
        mode.add_argument("--ml-threshold", type=float, help="Enable the ML entry filter")
    args = parser.parse_args()
    if not 2 <= args.period <= 198:
        parser.error("--period must be between 2 and 198")
    if args.ml_threshold is not None:
        if not 0 <= args.ml_threshold <= 1:
            parser.error("--ml-threshold must be between 0 and 1")
        if args.period > 149:
            parser.error("--ml-threshold requires --period of at most 149")
    if not all(math.isfinite(x) and x > 0 for x in (args.cash, args.deviations)):
        parser.error("--cash and --deviations must be finite and positive")
    if not (0 < args.allocation <= 1 and 0 <= args.fee < 1 and 0 <= args.slippage < 1):
        parser.error("--allocation must be in (0, 1], --fee and --slippage in [0, 1)")
    if args.mode == "paper" and args.poll < 1:
        parser.error("--poll must be at least 1 second")
    try:
        if args.mode == "paper":
            paper(args)
        else:
            candles = demo() if args.demo else read_csv(args.csv)
            if args.demo and args.ml_threshold is not None:
                start = candles[0].start
                warmup = [Candle(start - timedelta(minutes=15 * i), 100, 100)
                          for i in range(199, 0, -1)]
                candles = warmup + candles
            backtest(candles, args)
    except KeyboardInterrupt:
        return
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
