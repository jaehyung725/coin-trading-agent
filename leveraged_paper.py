"""Isolated-margin simulated long positions, kept in the demo account database."""

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from math import fsum, isfinite
import json
import sqlite3


@dataclass(frozen=True)
class Long:
    margin: float
    leverage: int
    entry: float
    fee: float
    slippage: float

    @property
    def notional(self):
        return self.margin * self.leverage

    @property
    def quantity(self):
        return self.notional / self.entry

    @property
    def entry_fee(self):
        return self.notional * self.fee

    @property
    def liquidation_price(self):
        return (self.notional - self.margin) / (self.quantity * self.exit_factor)

    @property
    def exit_factor(self):
        return (1 - self.slippage) * (1 - self.fee)

    def release(self, mark):
        return max(0.0, self.margin + self.quantity * mark * self.exit_factor - self.notional)


@dataclass(frozen=True)
class LongTotals:
    entries: int
    exits: int
    fees: float
    realized_pnl: float


def merge_legacy_cash(db, schema="main"):
    """Move the old wallet's net cash movements into the shared account once."""
    legacy_table = db.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name='leverage_wallet'"
    ).fetchone()
    if not legacy_table:
        return
    columns = {column[1] for column in db.execute(f"PRAGMA {schema}.table_info(leverage_wallet)")}
    if "merged" not in columns:
        db.execute(f"ALTER TABLE {schema}.leverage_wallet ADD COLUMN merged INTEGER NOT NULL DEFAULT 0")
    legacy = db.execute(f"SELECT cash FROM {schema}.leverage_wallet WHERE id=1 AND merged=0").fetchone()
    if legacy is not None:
        settings, = db.execute(f"SELECT settings FROM {schema}.account WHERE id=1").fetchone()
        seed = json.loads(settings)["cash"]
        db.execute(f"UPDATE {schema}.account SET cash=cash+? WHERE id=1", (legacy[0] - seed,))
        db.execute(f"UPDATE {schema}.leverage_wallet SET merged=1 WHERE id=1")


def attach_portfolio_accounts(db, directory, active_path):
    others = sorted(set(directory.glob("*.sqlite3")) - {active_path})
    if len(others) > db.getlimit(sqlite3.SQLITE_LIMIT_ATTACHED):
        raise ValueError("한 번에 사용할 수 있는 모의계좌 수를 초과했습니다.")
    schemas = ["main"]
    for index, path in enumerate(others):
        schema = f"account_{index}"
        db.execute(f"ATTACH DATABASE ? AS {schema}", (f"{path.as_uri()}?mode=rw",))
        schemas.append(schema)
    return schemas


def read_portfolio_balances(db, schemas):
    balances = {}
    for schema in schemas:
        merge_legacy_cash(db, schema)
        cash, = db.execute(f"SELECT cash FROM {schema}.account WHERE id=1").fetchone()
        balances[schema] = cash
    return balances


def record_cash_adjustment(db, schema, cash, target, timestamp):
    change = target - cash
    if change == 0:
        return
    db.execute(f"""CREATE TABLE IF NOT EXISTS {schema}.cash_adjustments (
        time TEXT PRIMARY KEY, amount REAL NOT NULL)""")
    db.execute(f"INSERT INTO {schema}.cash_adjustments VALUES (?, ?)", (timestamp, change))
    db.execute(f"UPDATE {schema}.account SET cash=? WHERE id=1", (target,))


def fund_long_order(db, balances, required, timestamp):
    if required > fsum(balances.values()):
        raise ValueError("증거금과 진입 수수료를 낼 전체 가용 현금이 부족합니다.")
    cash = balances["main"]
    remaining = required - cash
    if remaining <= 0:
        return cash
    for schema, balance in balances.items():
        if schema == "main" or balance <= 0:
            continue
        moved = min(balance, remaining)
        record_cash_adjustment(db, schema, balance, balance - moved, timestamp)
        remaining -= moved
        if remaining <= 0:
            break
    record_cash_adjustment(db, "main", cash, required, timestamp)
    return required


def paper_long(path, *, mark=None, open_long=None, close_long=False, portfolio_directory=None):
    """Load the wallet and atomically apply an order or a triggered liquidation."""
    if mark is not None and (not isfinite(mark) or mark <= 0):
        raise ValueError("유효한 시세가 필요합니다.")
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True)) as db, db:
        schemas = ["main"]
        if open_long is not None and portfolio_directory is not None:
            schemas = attach_portfolio_accounts(db, portfolio_directory, path)
        db.execute("BEGIN IMMEDIATE")
        balances = read_portfolio_balances(db, schemas)
        db.execute("""CREATE TABLE IF NOT EXISTS leverage_positions (
            id INTEGER PRIMARY KEY, opened_at TEXT NOT NULL, margin REAL NOT NULL,
            leverage INTEGER NOT NULL, entry REAL NOT NULL, fee REAL NOT NULL,
            slippage REAL NOT NULL, closed_at TEXT, exit REAL, pnl REAL, status TEXT,
            entry_cash REAL, exit_cash REAL
        )""")
        columns = {column[1] for column in db.execute("PRAGMA table_info(leverage_positions)")}
        if "entry_cash" not in columns:
            db.execute("ALTER TABLE leverage_positions ADD COLUMN entry_cash REAL")
        if "exit_cash" not in columns:
            db.execute("ALTER TABLE leverage_positions ADD COLUMN exit_cash REAL")
        db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS leverage_one_open
            ON leverage_positions ((1)) WHERE closed_at IS NULL""")
        cash = balances["main"]
        row = db.execute("""SELECT id, margin, leverage, entry, fee, slippage
            FROM leverage_positions WHERE closed_at IS NULL""").fetchone()
        position = Long(*row[1:]) if row else None
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        event = None
        if open_long is not None:
            if position is not None or mark is None:
                raise ValueError("유효한 시세와 빈 포지션이 필요합니다.")
            margin, leverage, fee, slippage = open_long
            if (not isfinite(margin) or margin <= 0 or not isinstance(leverage, int)
                    or not 1 <= leverage <= 50 or not 0 <= fee < 1 or not 0 <= slippage < 1):
                raise ValueError("증거금과 레버리지 배율을 확인하세요.")
            position = Long(margin, leverage, mark * (1 + slippage), fee, slippage)
            cost = position.margin + position.entry_fee
            if not isfinite(cost):
                raise ValueError("증거금과 진입 수수료를 낼 가용 잔고가 부족합니다.")
            if mark <= position.liquidation_price:
                raise ValueError("주문 가격이 모의 청산 조건에 해당합니다.")
            cash = fund_long_order(db, balances, cost, now) - cost
            db.execute("""INSERT INTO leverage_positions
                (opened_at, margin, leverage, entry, fee, slippage, entry_cash)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (now, margin, leverage, position.entry, fee, slippage, cash))
            event = "롱 진입"
        elif position is not None and mark is not None:
            liquidated = mark <= position.liquidation_price
            if close_long or liquidated:
                settled = 0.0 if liquidated else position.release(mark)
                pnl = settled - position.margin - position.entry_fee
                event = "강제청산" if liquidated else "롱 종료"
                cash += settled
                db.execute("""UPDATE leverage_positions SET closed_at=?, exit=?, pnl=?, status=?, exit_cash=?
                    WHERE id=?""", (now, mark * (1 - position.slippage), pnl, event, cash, row[0]))
                position = None
        if event:
            db.execute("UPDATE account SET cash=? WHERE id=1", (cash,))
        history = db.execute("""SELECT closed_at, leverage, entry, exit, pnl, status
            FROM leverage_positions WHERE closed_at IS NOT NULL ORDER BY id DESC LIMIT 20""").fetchall()
        totals = LongTotals(*db.execute("""
            SELECT COUNT(*), COUNT(closed_at),
                   COALESCE(SUM(margin * leverage * fee +
                       CASE WHEN status = '롱 종료'
                            THEN margin * leverage / entry * exit * fee ELSE 0 END), 0),
                   COALESCE(SUM(pnl), 0)
            FROM leverage_positions
        """).fetchone())
    return cash, position, history, event, totals
