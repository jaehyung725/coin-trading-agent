"""Save a one-off Bitcoin comparison from local synchronized account records."""

import argparse
import fcntl
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

from leveraged_paper import Position
from llm_strategy import AUTO_STRATEGIES
from strategies import STRATEGY_VERSION


KST = timezone(timedelta(hours=9))
REPORT_DIR = Path(__file__).resolve().parent / ".cache" / "reports"
JOB_FILE = REPORT_DIR / "btc-comparison-job.json"
STRATEGY_FILES = {
    name: "paper-auto-KRW-BTC.sqlite3" if name == "bollinger" and STRATEGY_VERSION == 1
    else f"paper-auto-KRW-BTC-{name}-v{STRATEGY_VERSION}.sqlite3"
    for name in AUTO_STRATEGIES
}


def atomic_write(path, contents):
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        temporary.write(contents)
    Path(temporary.name).replace(path)


def activation_times(directory):
    result = {}
    for name, filename in STRATEGY_FILES.items():
        path = directory / filename
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                row = db.execute("SELECT time FROM auto_run_log WHERE side='활성화' ORDER BY id LIMIT 1").fetchone()
                if row:
                    result[name] = row[0]
        except sqlite3.Error:
            continue
    return result


def account_at(path, anchors, today, cutoff):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        anchor = db.execute("SELECT time FROM auto_run_log WHERE side='활성화' ORDER BY id LIMIT 1").fetchone()
        if not anchor or anchor[0] != anchors:
            return None
        settings = json.loads(db.execute("SELECT settings FROM account WHERE id=1").fetchone()[0])
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        spot = db.execute("SELECT * FROM fills WHERE time<? ORDER BY time", (cutoff,)).fetchall()
        positions = db.execute("SELECT * FROM leverage_positions WHERE opened_at<? ORDER BY id", (cutoff,)).fetchall() if "leverage_positions" in tables else []
        last = db.execute("SELECT time, side FROM auto_run_log WHERE time<? ORDER BY id DESC LIMIT 1", (cutoff,)).fetchone()
        judgments, = db.execute("""SELECT COUNT(*) FROM auto_run_log WHERE time>=? AND time<?
                                      AND side NOT IN ('활성화', '종료', '재시작')""", (today, cutoff)).fetchone()
        mark = db.execute("SELECT time, mark FROM auto_decision WHERE time<? AND mark>0", (cutoff,)).fetchone() if "auto_decision" in tables else None

    cash = settings["cash"]
    units = 0.0
    entries = exits = 0
    fills = []
    for row in spot:
        change = row["units"] - units
        cash -= change * row["price"] + abs(change) * row["price"] * settings["fee"]
        units = row["units"]
        entries += today <= row["time"] and row["side"] == "BUY"
        exits += today <= row["time"] and row["side"] == "SELL"
        if today <= row["time"]:
            fills.append((row["time"], "현물 매수" if row["side"] == "BUY" else "현물 매도", row["price"], None))

    open_position = None
    for row in positions:
        side = row["side"] if "side" in row.keys() else "long"
        if today <= row["opened_at"]:
            fills.append((row["opened_at"], "롱 진입" if side == "long" else "숏 진입", row["entry"], None))
        if row["closed_at"] and row["closed_at"] < cutoff:
            cash += row["pnl"]
            exits += today <= row["closed_at"]
            if today <= row["closed_at"]:
                fills.append((row["closed_at"], row["status"], row["exit"], row["pnl"]))
        else:
            open_position = [row[key] for key in ("margin", "leverage", "entry", "fee", "slippage")] + [side]
            position = Position(*open_position)
            cash -= position.margin + position.entry_fee
        entries += today <= row["opened_at"]
    return {
        "last": list(last) if last else None,
        "mark": list(mark) if mark else None,
        "initial": settings["cash"], "cash": cash, "units": units, "open": open_position,
        "entries": entries, "exits": exits, "judgments": judgments, "fills": sorted(fills),
    }


def render_comparison(rows, start, cutoff):
    marks = [row["mark"] for row in rows.values() if row["mark"]]
    mark_time, price = max(marks, default=(None, None))
    if mark_time and cutoff - datetime.fromisoformat(mark_time) > timedelta(minutes=3):
        price = None
    lines = [
        "# 비트코인 자동매매 전략 수익률 비교", "",
        f"- 집계 구간 {start:%Y-%m-%d %H:%M} ~ {cutoff:%Y-%m-%d %H:%M} KST",
        f"- 공통 평가 시세 {price:,.2f}원 · {datetime.fromisoformat(mark_time).astimezone(KST):%H:%M:%S} KST"
        if price else "- 공통 평가 시세 미집계 · 마감 직전의 시세 기록 없음",
        "- 평가액과 수익률은 전략별 초기 자금 대비 누적 기준입니다. 진입·종료·판단 횟수는 위 구간 기준입니다.",
        "- 열린 포지션은 공통 시세로 평가하며 수수료와 슬리피지를 반영합니다.", "",
        "| 전략 | 평가액 | 순손익 | 수익률 | 진입 / 종료 | 판단 | 마지막 기록 (KST) |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    ranked = []
    for strategy, name in AUTO_STRATEGIES.items():
        row = rows.get(strategy)
        if not row:
            lines.append(f"| {name} | 미집계 | 미집계 | 미집계 | — | — | 계좌 기록 없음 |")
            continue
        equity = row["cash"]
        if row["units"] or row["open"]:
            equity = (equity + row["units"] * price + (Position(*row["open"]).release(price) if row["open"] else 0)
                      if price is not None else None)
        pnl = equity - row["initial"] if equity is not None else None
        pct = 100 * pnl / row["initial"] if pnl is not None and row["initial"] else None
        last = row["last"]
        last_text = f"{datetime.fromisoformat(last[0]).astimezone(KST):%H:%M:%S} · {last[1]}" if last else "기록 없음"
        amounts = f"{equity:,.2f}원 | {pnl:+,.2f}원 | {pct:+.4f}%" if pct is not None else "미집계 | 미집계 | 미집계"
        lines.append(f"| {name} | {amounts} | {row['entries']} / {row['exits']} | {row['judgments']} | {last_text} |")
        if pct is not None and row["entries"] and row["judgments"]:
            ranked.append((pct, name))
    if ranked:
        ranked.sort(reverse=True)
        lines += ["", "## 결과 요약", "", "체결과 평가 기록이 있는 전략의 누적 수익률 순위는 다음과 같습니다.", "",
                  *(f"{index}. {name} {pct:+.4f}%" for index, (pct, name) in enumerate(ranked, 1))]
    idle = [AUTO_STRATEGIES[key] for key, row in rows.items() if row["judgments"] == 0]
    if idle:
        lines += ["", "평가 구간에 전략 판단 기록이 없는 계좌는 성과 순위에서 제외했습니다. " + ", ".join(idle)]
    entries = [(timestamp, AUTO_STRATEGIES[key], kind, price, pnl)
               for key, row in rows.items() for timestamp, kind, price, pnl in row["fills"]]
    if entries:
        lines += ["", "## 당일 체결 기록", "", "| 시각 (KST) | 전략 | 체결 | 체결가 | 실현 손익 |",
                  "|---|---|---|---:|---:|"]
        for timestamp, name, kind, price, pnl in sorted(entries):
            when = datetime.fromisoformat(timestamp).astimezone(KST).strftime("%H:%M:%S")
            amount = f"{pnl:+,.2f}원" if pnl is not None else "—"
            lines.append(f"| {when} | {name} | {kind} | {price:,.2f}원 | {amount} |")
    return "\n".join(lines) + "\n"


def collect_report(directory=None, now=None):
    if not JOB_FILE.is_file():
        return None
    now = now or datetime.now(timezone.utc)
    with (REPORT_DIR / ".comparison.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        job = json.loads(JOB_FILE.read_text())
        output = REPORT_DIR / job["output"]
        if output.exists():
            return output
        cutoff = datetime.fromisoformat(job["cutoff"])
        start = cutoff - timedelta(days=1)
        checkpoint = REPORT_DIR / job["checkpoint"]
        rows = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
        directories = [directory] if directory else list(Path("/tmp").glob("paper-account-*"))
        for source in directories:
            for name, anchor in job["anchors"].items():
                path = source / STRATEGY_FILES[name]
                try:
                    row = account_at(path, anchor, start.astimezone(timezone.utc).isoformat(),
                                     cutoff.astimezone(timezone.utc).isoformat())
                except (sqlite3.Error, OSError, ValueError):
                    continue
                if row and (name not in rows or (row["last"] or [""])[0] >= (rows[name]["last"] or [""])[0]):
                    if not row["mark"] and name in rows:
                        row["mark"] = rows[name]["mark"]
                    rows[name] = row
        atomic_write(checkpoint, json.dumps(rows, ensure_ascii=False))
        if now >= cutoff and rows:
            atomic_write(output, render_comparison(rows, start, cutoff))
            return output
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--cutoff", type=datetime.fromisoformat)
    args = parser.parse_args()
    if args.source and args.cutoff:
        cutoff = args.cutoff.astimezone(KST)
        REPORT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        if JOB_FILE.exists():
            parser.error("A comparison is already registered")
        anchors = activation_times(args.source.resolve())
        if anchors.keys() != STRATEGY_FILES.keys():
            parser.error("The source must have activation records for all Bitcoin strategies")
        label = (cutoff - timedelta(seconds=1)).date().isoformat()
        atomic_write(JOB_FILE, json.dumps({
            "cutoff": cutoff.isoformat(), "anchors": anchors, "output": f"btc-comparison-{label}.md",
            "checkpoint": f".btc-comparison-{label}.json",
        }))
    if not JOB_FILE.exists():
        parser.error("No comparison registered")
    print("Bitcoin comparison watcher started", flush=True)
    while True:
        output = collect_report()
        if output:
            print(f"Comparison saved at {output}", flush=True)
            return
        time.sleep(20)


if __name__ == "__main__":
    main()
