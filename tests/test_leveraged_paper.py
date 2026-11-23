import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from agent import initialize_account
from dashboard import execution_events, portfolio_totals, read_account
from leveraged_paper import Position, paper_leverage, paper_long


class LeverageTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "paper.sqlite3"
        self.settings = {"market": "KRW-BTC", "cash": 1000, "fee": 0.001}
        with closing(sqlite3.connect(self.path)) as db, db:
            initialize_account(db, self.settings, 1000)

    def test_short_closed_profit_includes_slippage_and_both_fees(self):
        cash, position, _, event, _ = paper_leverage(
            self.path, mark=100, order=("short", 100, 5, 0.001, 0.002))
        self.assertEqual(event, "숏 진입")
        self.assertAlmostEqual(cash, 899.5)
        self.assertAlmostEqual(position.entry, 99.8)
        self.assertAlmostEqual(position.exit_price(90), 90.18)
        self.assertGreater(position.release(90) - position.margin - position.entry_fee, 0)
        self.assertLess(position.release(105) - position.margin - position.entry_fee, 0)
        wallet = paper_leverage(self.path)
        account, _, _ = read_account(self.path)
        totals, missing, unavailable = portfolio_totals(
            self.path.parent, self.path, account, self.settings, [], [], wallet, {"KRW-BTC": 90})
        self.assertFalse(missing)
        self.assertEqual(unavailable, 0)
        self.assertAlmostEqual(totals["cash"] + totals["position_value"], cash + position.release(90))
        cash_after, _, _, event, totals = paper_leverage(self.path, mark=90, close_side="short")
        self.assertEqual(event, "숏 종료")
        self.assertAlmostEqual(cash_after - 1000, totals.realized_pnl)
        self.assertAlmostEqual(totals.fees, position.entry_fee + position.quantity * 90.18 * 0.001)
        events = execution_events(self.path, "KRW-BTC", [], [])
        self.assertEqual([row["구분"] for _, row in events], ["숏 진입", "숏 종료"])
        self.assertTrue(all(row["출처"] == "수동" for _, row in events))

    def test_short_liquidates_at_upper_boundary_only_once(self):
        cash, position, _, _, _ = paper_leverage(
            self.path, mark=100, order=("short", 100, 5, 0.001, 0.002))
        threshold = position.liquidation_price
        self.assertGreater(threshold, 100)
        self.assertIsNotNone(paper_leverage(self.path, mark=threshold - 0.01)[1])
        final_cash, closed, _, event, totals = paper_leverage(self.path, mark=threshold)
        self.assertIsNone(closed)
        self.assertEqual(event, "숏 강제청산")
        self.assertAlmostEqual(final_cash, cash)
        self.assertAlmostEqual(totals.realized_pnl, -100.5)
        self.assertIsNone(paper_leverage(self.path, mark=threshold + 10)[3])
        self.assertEqual(paper_leverage(self.path)[4].exits, 1)

    def test_short_rejects_insufficient_margin_and_opposite_open(self):
        with self.assertRaises(ValueError):
            paper_leverage(self.path, mark=100, order=("short", 1000, 5, 0.001, 0))
        self.assertEqual(paper_leverage(self.path)[0], 1000)
        paper_long(self.path, mark=100, open_long=(100, 5, 0, 0))
        with self.assertRaises(ValueError):
            paper_leverage(self.path, mark=100, order=("short", 10, 5, 0, 0))
        with self.assertRaises(ValueError):
            paper_leverage(self.path, mark=100, close_side="short")
        cash, _, _, event, totals = paper_long(self.path, mark=110, close_long=True)
        self.assertEqual(event, "롱 종료")
        self.assertEqual(cash, 1050)
        self.assertEqual(totals.realized_pnl, 50)

    def test_previous_schema_defaults_existing_positions_to_long(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TABLE leverage_positions (
                id INTEGER PRIMARY KEY, opened_at TEXT NOT NULL, margin REAL NOT NULL,
                leverage INTEGER NOT NULL, entry REAL NOT NULL, fee REAL NOT NULL,
                slippage REAL NOT NULL, closed_at TEXT, exit REAL, pnl REAL, status TEXT)""")
            db.execute("""INSERT INTO leverage_positions (opened_at, margin, leverage, entry, fee, slippage)
                VALUES ('2026-01-01T00:00:00+00:00', 100, 5, 100, 0, 0)""")
            db.execute("UPDATE account SET cash=900 WHERE id=1")
        _, position, _, _, _ = paper_leverage(self.path)
        self.assertEqual(position, Position(100, 5, 100, 0, 0))
        self.assertEqual(position.liquidation_price, 80)
        self.assertIsNotNone(paper_leverage(self.path, mark=80.01)[1])
        self.assertEqual(paper_leverage(self.path, mark=80)[3], "강제청산")
        events = execution_events(self.path, "KRW-BTC", [], [])
        self.assertEqual([row["출처"] for _, row in events], ["미분류", "수동"])
