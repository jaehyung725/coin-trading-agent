import json
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agent import initialize_account
from scheduled_comparison import STRATEGY_FILES, collect_report


class ComparisonTests(unittest.TestCase):
    def test_saves_at_cutoff_and_ignores_later_trades(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cutoff = datetime.fromisoformat("2026-11-24T00:00:00+09:00")
            activation = "2026-11-23T08:00:00+00:00"
            before = "2026-11-23T14:59:50+00:00"
            after = "2026-11-23T15:00:10+00:00"
            job = root / "job.json"
            job.write_text(json.dumps({
                "cutoff": cutoff.isoformat(), "anchors": {"bollinger": activation},
                "output": "comparison.md", "checkpoint": "checkpoint.json",
            }))
            with closing(sqlite3.connect(root / STRATEGY_FILES["bollinger"])) as db, db:
                initialize_account(db, {"cash": 1000, "fee": 0}, 1000)
                db.execute("CREATE TABLE auto_run_log (id INTEGER PRIMARY KEY, time TEXT, side TEXT)")
                db.executemany("INSERT INTO auto_run_log(time, side) VALUES (?, ?)",
                               [(activation, "활성화"), (before, "롱 진입"), (after, "롱 종료")])
                db.execute("CREATE TABLE auto_decision (time TEXT, mark REAL)")
                db.execute("INSERT INTO auto_decision VALUES (?, 105)", (before,))
                db.execute("""CREATE TABLE leverage_positions (
                    id INTEGER PRIMARY KEY, opened_at TEXT, closed_at TEXT, side TEXT, margin REAL,
                    leverage INTEGER, entry REAL, exit REAL, fee REAL, slippage REAL, pnl REAL, status TEXT
                )""")
                db.execute("""INSERT INTO leverage_positions VALUES (
                    1, ?, ?, 'long', 100, 2, 100, 115, 0, 0, 30, '롱 종료'
                )""", (before, after))
            with (patch("scheduled_comparison.REPORT_DIR", root), patch("scheduled_comparison.JOB_FILE", job)):
                self.assertIsNone(collect_report(root, cutoff - timedelta(seconds=1)))
                output = collect_report(root, cutoff)
                self.assertEqual(collect_report(root, cutoff + timedelta(seconds=1)), output)
                report = output.read_text()
            self.assertIn("1,010.00원", report)
            self.assertIn("+1.0000%", report)
            self.assertIn("1 / 0", report)
            self.assertNotIn("롱 종료", report)
