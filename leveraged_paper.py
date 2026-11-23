"""Isolated-margin simulated long and short positions, kept in the demo account database."""

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from math import fsum, isfinite
import json
import sqlite3


@dataclass(frozen=True)
class Position:
    margin: float
    leverage: int
    entry: float
    fee: float
    slippage: float
    side: str = "long"

    @property
    def direction(self):
        return 1 if self.side == "long" else -1

    @property
    def label(self):
        return "롱" if self.side == "long" else "숏"

    @classmethod
    def at_market(cls, side, mark, margin, leverage, fee, slippage):
        direction = 1 if side == "long" else -1
        return cls(margin, leverage, mark * (1 + direction * slippage), fee, slippage, side)

    def exit_price(self, mark):
        return mark * (1 - self.direction * self.slippage)

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
        exit_factor = (1 - self.direction * self.slippage) * (1 - self.direction * self.fee)
        return (self.notional - self.direction * self.margin) / (self.quantity * exit_factor)

    def is_liquidated(self, mark):
        return self.direction * (mark - self.liquidation_price) <= 0

    def release(self, mark):
        if self.is_liquidated(mark):
            return 0.0
        exit_notional = self.quantity * self.exit_price(mark)
        gross_pnl = self.direction * (exit_notional - self.notional)
        return max(0.0, self.margin + gross_pnl - exit_notional * self.fee)


@dataclass(frozen=True)
class LeverageTotals:
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


def manual_account_paths(directory):
    return sorted(path for path in directory.glob("*.sqlite3") if not path.name.startswith("paper-auto-"))


def attach_portfolio_accounts(db, directory, active_path):
    others = sorted(set(manual_account_paths(directory)) - {active_path})
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


def fund_leverage_order(db, balances, required, timestamp):
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


def ensure_trade_source(db, table):
    # Existing fills have no reliable provenance.
    if "source" not in {column[1] for column in db.execute(f"PRAGMA table_info({table})")}:
        db.execute(f"ALTER TABLE {table} ADD COLUMN source TEXT")


def paper_leverage(path, *, mark=None, order=None, close_side=None, portfolio_directory=None):
    """Load the wallet and atomically apply an order or a triggered liquidation."""
    if close_side not in (None, "long", "short") or (order is not None and close_side is not None):
        raise ValueError("올바른 레버리지 주문이 필요합니다.")
    if mark is not None and (not isfinite(mark) or mark <= 0):
        raise ValueError("유효한 시세가 필요합니다.")
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True)) as db, db:
        schemas = ["main"]
        if order is not None and portfolio_directory is not None:
            schemas = attach_portfolio_accounts(db, portfolio_directory, path)
        db.execute("BEGIN IMMEDIATE")
        return leverage_in_transaction(db, schemas, mark=mark, order=order, close_side=close_side)


def leverage_in_transaction(db, schemas=("main",), *, mark=None, order=None, close_side=None, source="manual"):
    balances = read_portfolio_balances(db, schemas)
    db.execute("""CREATE TABLE IF NOT EXISTS leverage_positions (
        id INTEGER PRIMARY KEY, opened_at TEXT NOT NULL, margin REAL NOT NULL,
        leverage INTEGER NOT NULL, entry REAL NOT NULL, fee REAL NOT NULL,
        slippage REAL NOT NULL, closed_at TEXT, exit REAL, pnl REAL, status TEXT,
        entry_cash REAL, exit_cash REAL
    )""")
    ensure_trade_source(db, "leverage_positions")
    columns = {column[1] for column in db.execute("PRAGMA table_info(leverage_positions)")}
    if "entry_cash" not in columns:
        db.execute("ALTER TABLE leverage_positions ADD COLUMN entry_cash REAL")
    if "exit_cash" not in columns:
        db.execute("ALTER TABLE leverage_positions ADD COLUMN exit_cash REAL")
    if "exit_source" not in columns:
        db.execute("ALTER TABLE leverage_positions ADD COLUMN exit_source TEXT")
    if "side" not in columns:
        db.execute("""ALTER TABLE leverage_positions ADD COLUMN side TEXT NOT NULL DEFAULT 'long'
            CHECK(side IN ('long', 'short'))""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS leverage_one_open
        ON leverage_positions ((1)) WHERE closed_at IS NULL""")
    cash = balances["main"]
    row = db.execute("""SELECT id, margin, leverage, entry, fee, slippage, side
        FROM leverage_positions WHERE closed_at IS NULL""").fetchone()
    position = Position(*row[1:]) if row else None
    if close_side and (position is None or position.side != close_side or mark is None):
        raise ValueError("선택한 방향의 포지션과 유효한 시세가 필요합니다.")
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    event = None
    if order is not None:
        if position is not None or mark is None:
            raise ValueError("유효한 시세와 빈 포지션이 필요합니다.")
        side, margin, leverage, fee, slippage = order
        if (side not in ("long", "short") or not isfinite(margin) or margin <= 0 or not isinstance(leverage, int)
                or not 1 <= leverage <= 50 or not 0 <= fee < 1 or not 0 <= slippage < 1):
            raise ValueError("증거금과 레버리지 배율을 확인하세요.")
        position = Position.at_market(side, mark, margin, leverage, fee, slippage)
        cost = position.margin + position.entry_fee
        if not isfinite(cost) or not isfinite(position.notional) or not isfinite(position.entry) or position.entry <= 0:
            raise ValueError("증거금과 진입 수수료를 낼 가용 잔고가 부족합니다.")
        if position.is_liquidated(mark):
            raise ValueError("주문 가격이 모의 청산 조건에 해당합니다.")
        cash = fund_leverage_order(db, balances, cost, now) - cost
        db.execute("""INSERT INTO leverage_positions
            (opened_at, margin, leverage, entry, fee, slippage, entry_cash, source, side)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, margin, leverage, position.entry, fee, slippage, cash, source, side))
        event = f"{position.label} 진입"
    elif position is not None and mark is not None:
        liquidated = position.is_liquidated(mark)
        if close_side or liquidated:
            settled = 0.0 if liquidated else position.release(mark)
            pnl = settled - position.margin - position.entry_fee
            event = (("강제청산" if position.side == "long" else "숏 강제청산")
                     if liquidated else f"{position.label} 종료")
            cash += settled
            db.execute("""UPDATE leverage_positions SET closed_at=?, exit=?, pnl=?, status=?, exit_cash=?, exit_source=?
                WHERE id=?""", (now, position.exit_price(mark), pnl, event, cash, source, row[0]))
            position = None
    if event:
        db.execute("UPDATE account SET cash=? WHERE id=1", (cash,))
    history = db.execute("""SELECT closed_at, leverage, entry, exit, pnl, status
        FROM leverage_positions WHERE closed_at IS NOT NULL ORDER BY id DESC LIMIT 20""").fetchall()
    totals = LeverageTotals(*db.execute("""
        SELECT COUNT(*), COUNT(closed_at),
               COALESCE(SUM(margin * leverage * fee +
                   CASE WHEN status IN ('롱 종료', '숏 종료')
                        THEN margin * leverage / entry * exit * fee ELSE 0 END), 0),
               COALESCE(SUM(pnl), 0)
        FROM leverage_positions
    """).fetchone())
    return cash, position, history, event, totals


# Preserve the existing long-only order entry point for local callers.
Long = Position


def paper_long(path, *, mark=None, open_long=None, close_long=False, portfolio_directory=None):
    return paper_leverage(path, mark=mark, order=("long", *open_long) if open_long is not None else None,
                          close_side="long" if close_long else None, portfolio_directory=portfolio_directory)
