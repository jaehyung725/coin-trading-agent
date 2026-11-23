"""Store a scheduled Bitcoin comparison in the authenticated account snapshot."""

import sqlite3
from contextlib import closing
from datetime import timedelta, timezone

from scheduled_comparison import KST, STRATEGY_FILES, account_at, activation_times, render_comparison


def read_cloud_comparison(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='cloud_btc_comparison'").fetchone():
            return None
        return db.execute("SELECT cutoff, last_check, report FROM cloud_btc_comparison WHERE id=1").fetchone()


def schedule_cloud_comparison(path, cutoff):
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS cloud_btc_comparison (
            id INTEGER PRIMARY KEY CHECK(id=1), cutoff TEXT NOT NULL, last_check TEXT, report TEXT
        )""")
        db.execute("INSERT OR REPLACE INTO cloud_btc_comparison VALUES (1, ?, NULL, NULL)", (cutoff.isoformat(),))


def update_cloud_comparison(path, now, report=None):
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as db, db:
        db.execute("UPDATE cloud_btc_comparison SET last_check=?, report=? WHERE id=1", (now.isoformat(), report))


def build_cloud_comparison(directory, cutoff):
    cutoff = cutoff.astimezone(KST)
    start = cutoff - timedelta(days=1)
    rows = {}
    for name, anchor in activation_times(directory).items():
        row = account_at(directory / STRATEGY_FILES[name], anchor, start.astimezone(timezone.utc).isoformat(),
                         cutoff.astimezone(timezone.utc).isoformat())
        if row:
            rows[name] = row
    return render_comparison(rows, start, cutoff)
