"""Run opted-in paper strategies independently of Streamlit."""

import argparse
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError
from uuid import UUID
from zipfile import BadZipFile

from agent import history_size, paper_tick, recent_candles, record_hourly_return
from cloud_comparison import build_cloud_comparison, read_cloud_comparison, update_cloud_comparison
from llm_strategy import AUTO_STRATEGIES, LLM_STRATEGY
from operation_logs import operation_logger
from rapid_paper import rapid_tick
from scheduled_comparison import STRATEGY_FILES
from strategies import STRATEGY_VERSION
from supabase_accounts import (SupabaseError, load_config, publish_account_snapshot,
                               snapshot_rows, unpack)


LOGGER = logging.getLogger("coin_trading.worker")
POLL_SECONDS = 30
HEARTBEAT_MAX_AGE = timedelta(minutes=3)
MARKET_ERRORS = (URLError, TimeoutError, ValueError, KeyError, TypeError)


def config_table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def auto_enabled(db):
    if not config_table_exists(db, "auto_schedule"):
        return False
    row = db.execute("SELECT enabled FROM auto_schedule WHERE id=1").fetchone()
    return bool(row and row[0])


def set_auto_enabled(db, enabled):
    db.execute("""CREATE TABLE IF NOT EXISTS auto_schedule (
        id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)))""")
    db.execute("INSERT OR REPLACE INTO auto_schedule VALUES (1, ?)", (int(enabled),))


def heartbeat_path():
    return Path(os.environ.get("COIN_AUTO_HEARTBEAT", Path(__file__).resolve().parent / ".cache" / "auto-runner"))


def worker_online(now=None):
    now = now or datetime.now(timezone.utc)
    try:
        stamp = datetime.fromisoformat(heartbeat_path().read_text())
        return timedelta(0) <= now - stamp <= HEARTBEAT_MAX_AGE
    except (OSError, TypeError, ValueError):
        return False


def write_heartbeat():
    path = heartbeat_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as temp:
        temp.write(datetime.now(timezone.utc).isoformat())
    try:
        os.replace(temp.name, path)
    finally:
        Path(temp.name).unlink(missing_ok=True)


def cached_candle_loader():
    @lru_cache(maxsize=128)
    def fetch(market, minutes, boundary, count):
        return recent_candles(SimpleNamespace(market=market, minutes=minutes), boundary, count=count)

    def load(args, boundary, *, count=None):
        return fetch(args.market, args.minutes, boundary, count if count is not None else history_size(args) + 1)

    return load


def process_database(path, *, now, candle_loader):
    with closing(sqlite3.connect(path)) as db:
        if not auto_enabled(db):
            return
        row = db.execute("SELECT settings, last FROM account WHERE id=1").fetchone()
        settings = {"ml_threshold": None, "strategy": "bollinger", "strategy_version": 1, **json.loads(row[0])}
        if (settings.get("account_mode") != "auto" or settings["minutes"] != 60
                or settings["strategy"] not in AUTO_STRATEGIES
                or (settings["strategy"] == LLM_STRATEGY and not settings.get("rapid_profile")) or settings["strategy_version"] != STRATEGY_VERSION
                or settings["ml_threshold"] is not None or not 0 < settings["allocation"] <= 0.05):
            raise ValueError("Unsupported scheduled strategy settings")
        hour = now.replace(minute=0, second=0, microsecond=0)
        if not settings.get("rapid_profile") and row[1] and datetime.fromisoformat(row[1]) >= hour:
            return
        if config_table_exists(db, "auto_decision"):
            latest = db.execute("SELECT time, side FROM auto_decision WHERE id=1").fetchone()
            if latest and latest[1] == "SKIP" and now - datetime.fromisoformat(latest[0]) < timedelta(minutes=1):
                return
        if settings.get("rapid_profile"):
            rapid_tick(db, settings, now=now, candle_loader=candle_loader)
            return
        try:
            result = paper_tick(db, SimpleNamespace(**settings), candle_loader=candle_loader)
        except MARKET_ERRORS:
            # paper_tick commits the failure reason before raising.
            return
        if result is not None:
            with db:
                record_hourly_return(db, result["return_pct"], datetime.fromisoformat(result["time"]))


def process_snapshot(config, row, *, now, candle_loader, cloud_cutoff=None):
    user_id = str(UUID(row["user_id"]))
    files = unpack(row["snapshot"])
    auto_names = [name for name in files if re.fullmatch(r"paper-auto-KRW-[A-Z0-9]+(?:-[a-z0-9]+-v[0-9]+)?\.sqlite3", name)]
    if not auto_names and cloud_cutoff is None:
        return
    updated = {}
    with tempfile.TemporaryDirectory(prefix="paper-worker-") as directory:
        root = Path(directory)
        cloud_state = None
        if cloud_cutoff is not None:
            base = root / "paper.sqlite3"
            base.write_bytes(files[base.name])
            cloud_state = read_cloud_comparison(base)
            if (not cloud_state or cloud_state[2] is not None
                    or datetime.fromisoformat(cloud_state[0]) != cloud_cutoff):
                return
            auto_names = [name for name in auto_names if name in STRATEGY_FILES.values()]
        for name in auto_names:
            path = root / name
            path.write_bytes(files[name])
            if cloud_cutoff is not None and now >= cloud_cutoff:
                continue
            try:
                process_database(path, now=now, candle_loader=candle_loader)
            except (OSError, sqlite3.Error, KeyError, TypeError, ValueError) as error:
                LOGGER.warning("Could not evaluate automatic strategy file=%s type=%s", name, type(error).__name__)
                continue
            data = path.read_bytes()
            if data != files[name]:
                updated[name] = data
        if cloud_state:
            if now >= cloud_cutoff:
                report = build_cloud_comparison(root, cloud_cutoff)
                update_cloud_comparison(base, now, report)
                updated[base.name] = base.read_bytes()
            elif not cloud_state[1] or now - datetime.fromisoformat(cloud_state[1]) >= timedelta(minutes=1):
                update_cloud_comparison(base, now)
                updated[base.name] = base.read_bytes()
    if updated:
        try:
            publish_account_snapshot(config, user_id, row["revision"], {**files, **updated})
            LOGGER.info("Published automatic strategy updates count=%d", len(updated))
        except SupabaseError as error:
            if error.status != 409:
                raise
            LOGGER.info("Account changed concurrently; retrying from the stored revision on the next sweep")


def run_sweep(config, *, cloud_cutoff=None):
    after = None
    loader = cached_candle_loader()
    while True:
        filters = {"select": "user_id,revision,snapshot", "order": "user_id.asc", "limit": "25"}
        if after is not None:
            filters["user_id"] = f"gt.{after}"
        rows = snapshot_rows(config, filters)
        if not rows:
            return
        for row in rows:
            try:
                process_snapshot(config, row, now=datetime.now(timezone.utc), candle_loader=loader,
                                 cloud_cutoff=cloud_cutoff)
            except (BadZipFile, KeyError, TypeError, ValueError, sqlite3.Error, OSError):
                LOGGER.warning("Could not read a paper account snapshot")
        after = rows[-1]["user_id"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run one sweep and exit")
    parser.add_argument("--cloud-until", type=datetime.fromisoformat)
    parser.add_argument("--max-run-minutes", type=int)
    options = parser.parse_args()
    if options.cloud_until is not None and options.cloud_until.utcoffset() is None:
        parser.error("--cloud-until requires a timezone offset")
    deadline = time.monotonic() + 60 * options.max_run_minutes if options.max_run_minutes is not None else None
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    operation_logger("worker")
    LOGGER.info("Automatic trading worker started")
    config = load_config()
    if config is None:
        parser.error("Configure Supabase in .streamlit/secrets.toml")
    try:
        while True:
            try:
                run_sweep(config, cloud_cutoff=options.cloud_until)
                if not options.once:
                    write_heartbeat()
            except (SupabaseError, OSError) as error:
                LOGGER.warning("Paper runner sweep failed (%s)", getattr(error, "status", None) or "connection")
                if options.once:
                    return 1
            if options.once:
                return 0
            if options.cloud_until is not None and datetime.now(timezone.utc) >= options.cloud_until + timedelta(minutes=1):
                return 0
            if deadline is not None and time.monotonic() >= deadline:
                return 0
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
