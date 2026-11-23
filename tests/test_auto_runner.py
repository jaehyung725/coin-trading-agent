import json
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.error import URLError
from uuid import UUID

from agent import Candle, initialize_account
from auto_runner import process_snapshot, set_auto_enabled, worker_online, write_heartbeat
from cloud_comparison import read_cloud_comparison, schedule_cloud_comparison
from supabase_accounts import SupabaseError, pack, unpack


NAME = "paper-auto-KRW-BTC-sma20-v1.sqlite3"
USER = str(UUID(int=1))
NOW = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db_path = self.root / NAME
        self.manual = b"manual account retained byte for byte"
        with closing(sqlite3.connect(self.db_path)) as db, db:
            initialize_account(db, {
                "market": "KRW-BTC", "strategy": "sma20", "strategy_version": 1,
                "account_mode": "auto", "minutes": 60, "period": 20, "deviations": 2,
                "cash": 100_000, "allocation": 0.05, "fee": 0.001, "slippage": 0.001,
            }, 100_000)
            set_auto_enabled(db, True)
        self.candles = [Candle(NOW - timedelta(hours=22 - index), price, price)
                        for index, price in enumerate([100] * 20 + [90, 110, 123])]
        self.loader = Mock(return_value=self.candles)
        self.config = {}

    def row(self, *, revision=1, files=None):
        files = files or {"paper.sqlite3": self.manual, NAME: self.db_path.read_bytes()}
        return {"user_id": USER, "revision": revision, "snapshot": pack(files)}

    def sweep(self, row):
        with patch("agent.time.time", return_value=NOW.timestamp()):
            process_snapshot(self.config, row, now=NOW, candle_loader=self.loader)

    def test_runs_without_ui_preserves_manual_data_and_survives_restart(self):
        with patch("auto_runner.publish_account_snapshot") as publish:
            self.sweep(self.row())
            publish.assert_called_once()
            config, user, revision, files = publish.call_args.args
            self.assertEqual((config, user, revision), (self.config, USER, 1))
            self.assertEqual(files["paper.sqlite3"], self.manual)
            self.db_path.write_bytes(files[NAME])
            with closing(sqlite3.connect(self.db_path)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM returns_by_hour").fetchone()[0], 1)
            publish.reset_mock()
            self.sweep(self.row(revision=2, files=files))
            publish.assert_not_called()
        self.assertEqual(self.loader.call_count, 1)

    def test_stopped_revision_wins_over_inflight_worker(self):
        old = self.row()
        with closing(sqlite3.connect(self.db_path)) as db, db:
            set_auto_enabled(db, False)
        new = self.row(revision=2)
        with patch("auto_runner.publish_account_snapshot", side_effect=SupabaseError(409)) as publish:
            self.sweep(old)
            self.assertEqual(publish.call_args.args[2], 1)
            publish.reset_mock()
            self.sweep(new)
            publish.assert_not_called()
        self.assertEqual(self.loader.call_count, 1)
        self.assertEqual(unpack(new["snapshot"])[NAME], self.db_path.read_bytes())

    def test_background_entries_use_saved_leverage_and_include_fee_in_budget(self):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            settings = json.loads(db.execute("SELECT settings FROM account WHERE id=1").fetchone()[0])
            settings.update(rapid_profile=1, rapid_leverage=20, rapid_entry_percent=100)
            db.execute("UPDATE account SET settings=? WHERE id=1", (json.dumps(settings),))
        with (patch("rapid_paper.rapid_market", return_value=(NOW, 100, "long", "long")),
              patch("auto_runner.publish_account_snapshot") as publish):
            self.sweep(self.row())
            self.db_path.write_bytes(publish.call_args.args[3][NAME])
        with closing(sqlite3.connect(self.db_path)) as db:
            leverage, margin, entry, fee = db.execute(
                "SELECT leverage, margin, entry, fee FROM leverage_positions"
            ).fetchone()
            cash = db.execute("SELECT cash FROM account").fetchone()[0]
        self.assertEqual(leverage, 20)
        self.assertGreater(entry, 0)
        self.assertAlmostEqual(margin * (1 + leverage * fee), 100_000)
        self.assertAlmostEqual(cash, 0)

    def test_failed_market_data_publishes_explanation_without_fill(self):
        self.loader.side_effect = URLError("fixture")
        with patch("auto_runner.publish_account_snapshot") as publish:
            self.sweep(self.row())
            self.db_path.write_bytes(publish.call_args.args[3][NAME])
        with closing(sqlite3.connect(self.db_path)) as db:
            side, reason = db.execute("SELECT side, reason FROM auto_run_log").fetchone()
            self.assertEqual(side, "SKIP")
            self.assertIn("시세", reason)
            self.assertEqual(db.execute("SELECT count(*) FROM fills").fetchone()[0], 0)

    def test_heartbeat_expires(self):
        with patch.dict("os.environ", {"COIN_AUTO_HEARTBEAT": str(self.root / "heartbeat")}):
            self.assertFalse(worker_online())
            write_heartbeat()
            self.assertTrue(worker_online())
            self.assertFalse(worker_online(datetime.now(timezone.utc) + timedelta(minutes=4)))

    def test_cloud_comparison_is_published_to_the_same_account_at_cutoff(self):
        base = self.root / "paper.sqlite3"
        with closing(sqlite3.connect(base)) as db, db:
            initialize_account(db, {"cash": 1000}, 1000)
        schedule_cloud_comparison(base, NOW)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("CREATE TABLE auto_run_log (id INTEGER PRIMARY KEY, time TEXT, side TEXT)")
            db.execute("INSERT INTO auto_run_log(time, side) VALUES (?, '활성화')", ((NOW - timedelta(hours=1)).isoformat(),))
        row = self.row(files={base.name: base.read_bytes(), NAME: self.db_path.read_bytes()})
        with patch("auto_runner.publish_account_snapshot") as publish:
            process_snapshot(self.config, row, now=NOW, candle_loader=self.loader, cloud_cutoff=NOW)
            publish.assert_called_once()
            _, user_id, revision, files = publish.call_args.args
        self.loader.assert_not_called()
        self.assertEqual((user_id, revision), (USER, 1))
        self.assertEqual(files[NAME], self.db_path.read_bytes())
        base.write_bytes(files[base.name])
        _, _, report = read_cloud_comparison(base)
        self.assertIn("SMA 20", report)
        self.assertIn("+0.0000%", report)
