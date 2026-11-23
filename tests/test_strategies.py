import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import URLError

from agent import Account, Candle, decision, initialize_account, paper_tick, replay


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def candles(prices):
    return [Candle(START + timedelta(hours=index), price, price)
            for index, price in enumerate(prices)]


def settings(strategy):
    return SimpleNamespace(strategy=strategy, period=20, deviations=2, minutes=60,
                           ml_threshold=None, cash=100_000, allocation=0.05,
                           fee=0.001, slippage=0.001)


class StrategyTests(unittest.TestCase):
    def test_buy_sell_and_insufficient_history_for_every_strategy(self):
        for strategy, buy, sell in (
            ("bollinger", [100] * 20 + [80, 98], [100] * 21),
            ("sma20", [100] * 20 + [90, 110], [100] * 20 + [110, 90]),
            ("ema20", [100] * 100 + [90, 110], [100] * 100 + [110, 90]),
            ("rsi14", [100] * 85 + list(range(99, 84, -1)) + [95],
             [100] * 85 + list(range(101, 116)) + [105]),
            ("macd", [100] * 100 + [90, 120], [100] * 100 + [110, 80]),
        ):
            with self.subTest(strategy=strategy):
                args = settings(strategy)
                self.assertEqual(decision(candles(buy), Account(args.cash), args)[0], "BUY")
                self.assertEqual(decision(candles(sell), Account(args.cash, units=1), args)[0], "SELL")
                self.assertIsNone(decision(candles([100]), Account(args.cash), args)[0])

    def test_replay_matches_paper_tick_and_caps_buy_with_fees(self):
        args = settings("sma20")
        data = candles([100] * 20 + [90, 110, 123])
        steps, summary = replay(data, args)
        order = steps[-1]
        self.assertEqual(order["side"], "BUY")
        self.assertEqual(order["candle"], data[-2].start.isoformat())
        self.assertAlmostEqual(order["price"], 123 * 1.001)
        self.assertAlmostEqual(order["price"] * order["quantity"] + order["fee"], args.cash * 0.05)
        self.assertLess(summary["return_pct"], 0)
        with closing(sqlite3.connect(":memory:")) as db:
            with db:
                initialize_account(db, vars(args), args.cash)
            with patch("agent.time.time", return_value=data[-1].start.timestamp()), patch(
                    "agent.recent_candles", return_value=data):
                result = paper_tick(db, args)
                self.assertEqual(result["side"], "BUY")
                self.assertIsNone(paper_tick(db, args))
            records = db.execute("SELECT side, quantity, price, fee FROM auto_run_log").fetchall()
            self.assertEqual(records, [("BUY", order["quantity"], order["price"], order["fee"])])

    def test_gap_and_api_error_skip_orders_and_log_reason(self):
        args = settings("sma20")
        data = candles([100] * 21 + [90, 110, 120])
        data.pop(2)
        steps, _ = replay(data, args)
        self.assertEqual(steps[0]["side"], "SKIP")
        with closing(sqlite3.connect(":memory:")) as db:
            with db:
                initialize_account(db, vars(args), args.cash)
            with patch("agent.time.time", return_value=data[-1].start.timestamp()), patch(
                    "agent.recent_candles", return_value=data):
                self.assertIsNone(paper_tick(db, args))
            with patch("agent.recent_candles", side_effect=URLError("test")):
                with self.assertRaises(URLError):
                    paper_tick(db, args)
            logs = db.execute("SELECT side, reason FROM auto_run_log").fetchall()
            self.assertEqual([side for side, _ in logs], ["SKIP", "SKIP"])
            self.assertTrue(all(reason for _, reason in logs))
            self.assertEqual(db.execute("SELECT count(*) FROM fills").fetchone()[0], 0)
