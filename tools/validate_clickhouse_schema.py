"""Apply db/clickhouse/*.sql to an embedded ClickHouse (chDB) and smoke-test the views.

Usage: python tools/validate_clickhouse_schema.py db/clickhouse/001_market_data.sql
Requires: pip install chdb
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from chdb import session


def statements(sql: str) -> list[str]:
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


def main(path: str) -> int:
    with tempfile.TemporaryDirectory() as work:
        s = session.Session(work)
        stmts = statements(Path(path).read_text(encoding="utf-8"))
        failed = 0
        for stmt in stmts:
            try:
                s.query(stmt)
            except Exception as exc:  # report every failing statement, not only the first
                failed += 1
                print(f"FAILED: {stmt.splitlines()[0]}\n   -> {str(exc).splitlines()[0][:300]}")
        print(f"{len(stmts) - failed}/{len(stmts)} statements applied")
        if failed:
            return 1
        rows = [
            ("1", "10:00:01.100", "100.0", "2.0", "false", "10", "11"),
            ("2", "10:00:30.000", "101.5", "1.0", "true", "12", "12"),
            ("3", "10:00:59.900", "99.5", "3.0", "true", "13", "15"),
        ]
        day = "2026-11-23"
        values = ", ".join(
            f"('BTCUSDT', {i}, '{day} {t}', '{day} {t}', '{day} {t}', {p}, {q}, {m}, {f}, {last}, 'ws')"
            for i, t, p, q, m, f, last in rows
        )
        s.query(
            "INSERT INTO md.agg_trades (symbol, agg_trade_id, trade_ts, event_ts, recv_ts, price, qty, "
            f"is_buyer_maker, first_trade_id, last_trade_id, source) VALUES {values}"
        )
        row = str(
            s.query(
                "SELECT argMinMerge(open), max(high), min(low), argMaxMerge(close), sum(volume), "
                "sum(buy_volume), sum(sell_volume), sum(trades) FROM md.bars_1m",
                "CSV",
            )
        ).strip()
        expected = "100,101.5,99.5,99.5,6,2,4,6"
        print(f"bars_1m smoke test: {row} (expected {expected})")
        return 0 if row == expected else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
