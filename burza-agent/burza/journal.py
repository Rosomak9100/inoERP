"""Auditní deník v SQLite. Záznamy se pouze přidávají; uzavření obchodu doplní řádek."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, level TEXT NOT NULL,
    kind TEXT NOT NULL, message TEXT NOT NULL, data TEXT);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, symbol TEXT NOT NULL,
    side TEXT NOT NULL, entry REAL, stop REAL, target REAL, strategy TEXT,
    reason TEXT, accepted INTEGER NOT NULL, decision TEXT);
CREATE TABLE IF NOT EXISTS orders (
    client_ref TEXT PRIMARY KEY, ts TEXT NOT NULL, symbol TEXT NOT NULL,
    instrument_id INTEGER NOT NULL, action TEXT NOT NULL, amount REAL,
    stop REAL, target REAL, status TEXT NOT NULL, order_id TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS trades (
    position_id TEXT PRIMARY KEY, client_ref TEXT, symbol TEXT NOT NULL,
    instrument_id INTEGER NOT NULL, mode TEXT NOT NULL, open_ts TEXT, open_price REAL,
    units REAL, stop REAL, target REAL, close_ts TEXT, close_price REAL,
    pnl REAL, pnl_source TEXT, close_reason TEXT);
CREATE TABLE IF NOT EXISTS equity (
    ts TEXT NOT NULL, equity REAL NOT NULL, cash REAL NOT NULL, open_pnl REAL NOT NULL);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, publisher TEXT, symbol TEXT,
    headline TEXT NOT NULL, summary TEXT, url TEXT, published_at TEXT, fetched_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ai_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, symbol TEXT NOT NULL,
    candidate TEXT NOT NULL, take INTEGER NOT NULL, confidence REAL, size_factor REAL,
    decision TEXT, news_ids TEXT, macro TEXT, model TEXT, request_id TEXT,
    input_tokens INTEGER, output_tokens INTEGER, error TEXT);
"""

# Sloupce přidané v pozdějších verzích – doplní se do existující databáze.
MIGRATIONS = [
    ("trades", "costs", "REAL"),
    ("equity", "benchmark", "REAL"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Journal:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        for table, column, kind in MIGRATIONS:
            cols = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # --- obecné -------------------------------------------------------------------
    def event(self, kind: str, message: str, level: str = "INFO", data: dict | None = None) -> None:
        self.db.execute("INSERT INTO events(ts, level, kind, message, data) VALUES (?,?,?,?,?)",
                        (now_iso(), level, kind, message, json.dumps(data, default=str) if data else None))
        self.db.commit()

    def get_state(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_state(self, key: str, value) -> None:
        self.db.execute("INSERT INTO state(key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value, default=str)))
        self.db.commit()

    # --- signály a objednávky -----------------------------------------------------
    def signal(self, sig, strategy: str, accepted: bool, decision: str) -> None:
        self.db.execute(
            "INSERT INTO signals(ts, symbol, side, entry, stop, target, strategy, reason, accepted, decision)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), sig.symbol, sig.side, sig.entry, sig.stop, sig.target, strategy,
             sig.reason, int(accepted), decision))
        self.db.commit()

    def order_intent(self, client_ref: str, symbol: str, instrument_id: int, action: str,
                     amount: float | None, stop: float | None, target: float | None) -> None:
        """Zapíše se PŘED odesláním – po pádu lze dohledat podle client_ref (x-request-id)."""
        self.db.execute(
            "INSERT INTO orders(client_ref, ts, symbol, instrument_id, action, amount, stop, target, status)"
            " VALUES (?,?,?,?,?,?,?,?, 'pending')",
            (client_ref, now_iso(), symbol, instrument_id, action, amount, stop, target))
        self.db.commit()

    def order_update(self, client_ref: str, status: str, order_id: str | None = None,
                     raw: dict | None = None) -> None:
        self.db.execute("UPDATE orders SET status=?, order_id=COALESCE(?, order_id), raw=? WHERE client_ref=?",
                        (status, order_id, json.dumps(raw, default=str) if raw else None, client_ref))
        self.db.commit()

    def pending_orders(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM orders WHERE status='pending' AND action='open'").fetchall()

    # --- obchody ------------------------------------------------------------------
    def trade_open(self, position_id: str, client_ref: str, symbol: str, instrument_id: int,
                   mode: str, open_price: float, units: float, stop: float | None,
                   target: float | None, open_ts: str | None = None) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO trades(position_id, client_ref, symbol, instrument_id, mode, open_ts,"
            " open_price, units, stop, target) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (position_id, client_ref, symbol, instrument_id, mode, open_ts or now_iso(),
             open_price, units, stop, target))
        self.db.commit()

    def trade_close(self, position_id: str, close_price: float | None, pnl: float | None,
                    pnl_source: str, reason: str, close_ts: str | None = None,
                    costs: float | None = None) -> None:
        self.db.execute(
            "UPDATE trades SET close_ts=?, close_price=?, pnl=?, pnl_source=?, close_reason=?, costs=?"
            " WHERE position_id=? AND close_ts IS NULL",
            (close_ts or now_iso(), close_price, pnl, pnl_source, reason, costs, position_id))
        self.db.commit()

    def trade_set_exact(self, position_id: str, close_price: float, pnl: float, close_ts: str,
                        costs: float | None) -> None:
        """Nahradí odhad přesným výsledkem z historie eToro (původní hodnota zůstane v events)."""
        self.db.execute(
            "UPDATE trades SET close_price=?, pnl=?, close_ts=?, costs=?, pnl_source='eToro' WHERE position_id=?",
            (close_price, pnl, close_ts, costs, position_id))
        self.db.commit()

    def trades_needing_exact(self, mode: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM trades WHERE mode=? AND close_ts IS NOT NULL"
                               " AND pnl_source='odhad'", (mode,)).fetchall()

    def open_trades(self, mode: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM trades WHERE close_ts IS NULL AND mode=?", (mode,)).fetchall()

    def closed_trades(self, since: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM trades WHERE close_ts IS NOT NULL"
        args: tuple = ()
        if since:
            q += " AND close_ts >= ?"
            args = (since,)
        return self.db.execute(q + " ORDER BY close_ts", args).fetchall()

    def equity_point(self, equity: float, cash: float, open_pnl: float,
                     benchmark: float | None = None) -> None:
        self.db.execute("INSERT INTO equity(ts, equity, cash, open_pnl, benchmark) VALUES (?,?,?,?,?)",
                        (now_iso(), equity, cash, open_pnl, benchmark))
        self.db.commit()

    def equity_series(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM equity ORDER BY ts").fetchall()

    def rejected_signals(self, since: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM signals WHERE accepted=0 AND ts >= ? ORDER BY ts",
                               (since,)).fetchall()

    # --- Studie B -----------------------------------------------------------------
    def news_item(self, item) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO news(id, source, publisher, symbol, headline, summary, url,"
            " published_at, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (item.id, item.source, item.publisher, item.symbol, item.headline, item.summary,
             item.url, item.published_at.isoformat() if item.published_at else None, now_iso()))
        self.db.commit()

    def ai_decision(self, symbol: str, candidate: dict, take: bool, confidence: float | None,
                    size_factor: float | None, decision: dict | None, news_ids: list[str],
                    macro: dict | None, model: str, request_id: str | None,
                    input_tokens: int | None, output_tokens: int | None, error: str | None) -> None:
        self.db.execute(
            "INSERT INTO ai_decisions(ts, symbol, candidate, take, confidence, size_factor, decision,"
            " news_ids, macro, model, request_id, input_tokens, output_tokens, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), symbol, json.dumps(candidate, default=str), int(take), confidence, size_factor,
             json.dumps(decision, ensure_ascii=False, default=str) if decision else None,
             json.dumps(news_ids), json.dumps(macro, default=str) if macro else None, model,
             request_id, input_tokens, output_tokens, error))
        self.db.commit()

    def ai_calls_since(self, since: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM ai_decisions WHERE ts >= ? AND error IS NULL"
                               " AND model != 'pravidlo'", (since,)).fetchone()[0]
