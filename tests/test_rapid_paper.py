import io
import json
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.error import URLError

from agent import Candle, initialize_account
from rapid_paper import rapid_tick


NOW = datetime(2026, 1, 1, 10, 5, tzinfo=timezone.utc)
SETTINGS = dict(strategy="sma20", market="KRW-BTC", cash=100_000, fee=0.001,
                slippage=0.001, period=20, deviations=2)


def prices(bias, momentum, mark):
    def fetch(args, boundary, *, count):
        if args.minutes == 60:
            values = [100 + bias * index for index in range(count)]
        else:
            values = [100 + momentum * index for index in range(count - 1)] + [mark]
        return [Candle(boundary - timedelta(minutes=args.minutes * (count - index)), p, p)
                for index, p in enumerate(values)]
    return fetch


class RapidTests(unittest.TestCase):
    def test_llm_uses_closed_chart_on_schedule_and_checks_stops_without_model(self):
        settings = {**SETTINGS, "strategy": "llm", "rapid_leverage": 2, "rapid_entry_percent": 20}
        mark = 100

        def market(args, end, *, count):
            return [Candle(end - timedelta(minutes=args.minutes * (count - i)), mark, mark,
                           mark, mark, 10) for i in range(count)]

        def response(req, **_):
            request = json.loads(req.data)
            self.assertFalse(request["store"])
            self.assertTrue(request["text"]["format"]["strict"])
            data = json.loads(request["input"])
            self.assertEqual(data["closed_minutes"][-1][0], (NOW - timedelta(minutes=1)).isoformat())
            self.assertIn("RSI 14", data["hourly_indicators"])
            self.assertIsNone(data["position"])
            decision = json.dumps({"action": "long", "reason": "테스트 상승 신호",
                                   "leverage": 10, "entry_percent": 40})
            return io.BytesIO(json.dumps({"status": "completed", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": decision}]}
            ]}).encode())

        with (closing(sqlite3.connect(":memory:")) as db,
              patch("llm_strategy.openai_settings", return_value=("fake-test-key", "test-model")),
              patch("llm_strategy.urlopen", side_effect=response) as api):
            with db:
                initialize_account(db, settings, settings["cash"])
            rapid_tick(db, settings, now=NOW - timedelta(minutes=1), candle_loader=market)
            api.assert_not_called()
            rapid_tick(db, settings, now=NOW, candle_loader=market)
            rapid_tick(db, settings, now=NOW, candle_loader=market)
            self.assertEqual(db.execute("SELECT leverage, source FROM leverage_positions").fetchone(), (10, "auto"))
            self.assertAlmostEqual(db.execute("SELECT cash FROM account").fetchone()[0], 60_000)
            self.assertEqual(db.execute("SELECT reason FROM auto_decision").fetchone()[0],
                             "레버리지 10배 · 가용 현금 40% · 테스트 상승 신호")
            mark = 99
            rapid_tick(db, settings, now=NOW + timedelta(minutes=1), candle_loader=market)
            self.assertEqual(db.execute("SELECT status FROM leverage_positions").fetchone()[0], "롱 종료")
            self.assertEqual(db.execute("SELECT reason FROM auto_decision").fetchone()[0], "가격 기준 손절 도달")
            api.assert_called_once()

    def test_llm_rejects_sizing_outside_supported_values(self):
        def market(args, end, *, count):
            return [Candle(end - timedelta(minutes=args.minutes * (count - i)), 100, 100) for i in range(count)]

        settings = {**SETTINGS, "strategy": "llm"}
        decision = json.dumps({"action": "long", "reason": "테스트", "leverage": 50, "entry_percent": 40})
        result = {"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": decision}]}
        ]}
        with (closing(sqlite3.connect(":memory:")) as db,
              patch("llm_strategy.openai_settings", return_value=("fake-test-key", "test-model")),
              patch("llm_strategy.urlopen", return_value=io.BytesIO(json.dumps(result).encode())) as api):
            with db:
                initialize_account(db, settings, settings["cash"])
            rapid_tick(db, settings, now=NOW, candle_loader=market)
            api.assert_called_once()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM leverage_positions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT side FROM auto_decision").fetchone()[0], "HOLD")

    def test_immediate_long_and_short_risk_budget_and_no_duplicate(self):
        for direction in (1, -1):
            with self.subTest(direction=direction), closing(sqlite3.connect(":memory:")) as db:
                with db:
                    initialize_account(db, SETTINGS, SETTINGS["cash"])
                loader = prices(direction, direction, 100)
                rapid_tick(db, SETTINGS, now=NOW, candle_loader=loader)
                rapid_tick(db, SETTINGS, now=NOW, candle_loader=loader)
                positions = db.execute("SELECT side, margin, leverage, source FROM leverage_positions").fetchall()
                self.assertEqual(len(positions), 1)
                side, margin, leverage, source = positions[0]
                self.assertEqual(side, "long" if direction > 0 else "short")
                self.assertEqual((leverage, source), (2, "auto"))
                self.assertAlmostEqual(margin * (1 + leverage * SETTINGS["fee"]), 5000)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM auto_run_log").fetchone()[0], 1)

    def test_next_minute_closes_position_after_adverse_price(self):
        with closing(sqlite3.connect(":memory:")) as db:
            with db:
                initialize_account(db, SETTINGS, SETTINGS["cash"])
            rapid_tick(db, SETTINGS, now=NOW, candle_loader=prices(-1, -1, 100))
            rapid_tick(db, SETTINGS, now=NOW + timedelta(minutes=1), candle_loader=prices(-1, -1, 101))
            status, pnl, source = db.execute("SELECT status, pnl, exit_source FROM leverage_positions").fetchone()
            self.assertEqual((status, source), ("숏 종료", "auto"))
            self.assertLess(pnl, 0)

    def test_disagreement_and_market_error_do_not_open_position(self):
        def unavailable(*args, **kwargs):
            raise URLError("fixture")

        with closing(sqlite3.connect(":memory:")) as db:
            with db:
                initialize_account(db, SETTINGS, SETTINGS["cash"])
            rapid_tick(db, SETTINGS, now=NOW, candle_loader=prices(1, -1, 100))
            rapid_tick(db, SETTINGS, now=NOW + timedelta(minutes=1), candle_loader=unavailable)
            actions = db.execute("SELECT side FROM auto_run_log ORDER BY id").fetchall()
            self.assertEqual(actions, [("HOLD",), ("SKIP",)])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM leverage_positions").fetchone()[0], 0)
