"""
database.py – SQLite persistence layer.

Tables
──────
trades       – all executed (simulated or live) trades
bot_logs     – timestamped bot activity log
bot_status   – singleton row tracking current bot state
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.getenv("TRADING_DB_PATH", str(Path(__file__).parent / "trading_bot.db")))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create tables if they don't exist."""
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT    NOT NULL,
                direction   TEXT    NOT NULL,  -- LONG / SHORT
                entry_price REAL    NOT NULL,
                exit_price  REAL,
                size        REAL    NOT NULL,
                sl_price    REAL,
                tp_price    REAL,
                pnl         REAL,
                status      TEXT    NOT NULL DEFAULT 'OPEN',  -- OPEN / CLOSED
                opened_at   TEXT    NOT NULL,
                closed_at   TEXT,
                leverage    INTEGER NOT NULL DEFAULT 5,
                notes       TEXT
            );

            CREATE TABLE IF NOT EXISTS bot_logs (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                level     TEXT NOT NULL DEFAULT 'INFO',
                message   TEXT NOT NULL,
                ts        TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bot_status (
                id            INTEGER PRIMARY KEY CHECK (id = 1),
                running       INTEGER NOT NULL DEFAULT 0,
                symbol        TEXT    NOT NULL DEFAULT 'BTC-USDT',
                last_signal   TEXT    NOT NULL DEFAULT 'NONE',
                signal_hint   TEXT    NOT NULL DEFAULT 'WAIT',
                waiting_for   TEXT    NOT NULL DEFAULT 'Collecting candles',
                long_ready    INTEGER NOT NULL DEFAULT 0,
                short_ready   INTEGER NOT NULL DEFAULT 0,
                last_price    REAL,
                equity        REAL,
                open_trades   INTEGER NOT NULL DEFAULT 0,
                total_trades  INTEGER NOT NULL DEFAULT 0,
                win_trades    INTEGER NOT NULL DEFAULT 0,
                updated_at    TEXT    NOT NULL
            );

            INSERT OR IGNORE INTO bot_status (id, updated_at)
            VALUES (1, datetime('now'));
            """
        )

        # Lightweight migrations for existing DBs.
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(bot_status)").fetchall()
        }
        if "signal_hint" not in cols:
            conn.execute("ALTER TABLE bot_status ADD COLUMN signal_hint TEXT NOT NULL DEFAULT 'WAIT'")
        if "waiting_for" not in cols:
            conn.execute("ALTER TABLE bot_status ADD COLUMN waiting_for TEXT NOT NULL DEFAULT 'Collecting candles'")
        if "long_ready" not in cols:
            conn.execute("ALTER TABLE bot_status ADD COLUMN long_ready INTEGER NOT NULL DEFAULT 0")
        if "short_ready" not in cols:
            conn.execute("ALTER TABLE bot_status ADD COLUMN short_ready INTEGER NOT NULL DEFAULT 0")


# ── Trade operations ──────────────────────────────────────────────────────────

def open_trade(
    symbol: str,
    direction: str,
    entry_price: float,
    size: float,
    sl_price: float,
    tp_price: float,
    leverage: int,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO trades
                (symbol, direction, entry_price, size, sl_price, tp_price,
                 leverage, status, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
            """,
            (symbol, direction, entry_price, size, sl_price, tp_price, leverage, now),
        )
        return cur.lastrowid


def close_trade(trade_id: int, exit_price: float, pnl: float) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE trades
            SET exit_price = ?, pnl = ?, status = 'CLOSED', closed_at = ?
            WHERE id = ?
            """,
            (exit_price, pnl, now, trade_id),
        )


def get_open_trades(symbol: str | None = None) -> list[dict]:
    with _connect() as conn:
        if symbol:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status='OPEN' AND symbol=?", (symbol,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status='OPEN'"
            ).fetchall()
    return [dict(r) for r in rows]


def get_trade_history(symbol: str | None = None, limit: int = 100) -> list[dict]:
    with _connect() as conn:
        if symbol:
            rows = conn.execute(
                "SELECT * FROM trades WHERE symbol=? ORDER BY id DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


def get_trade_stats() -> dict:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*)                                     AS total,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)   AS wins,
                SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END)  AS losses,
                COALESCE(SUM(pnl), 0)                       AS total_pnl,
                COALESCE(AVG(pnl), 0)                       AS avg_pnl,
                COALESCE(MAX(pnl), 0)                       AS best_trade,
                COALESCE(MIN(pnl), 0)                       AS worst_trade
            FROM trades
            WHERE status = 'CLOSED'
            """
        ).fetchone()
    stats = dict(row)
    total = stats["total"] or 1
    wins = stats["wins"] or 0
    stats["win_rate"] = round(wins / total * 100, 2)
    return stats


# ── Logging ───────────────────────────────────────────────────────────────────

def log_event(message: str, level: str = "INFO") -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO bot_logs (level, message, ts) VALUES (?, ?, ?)",
            (level, message, now),
        )


def get_logs(limit: int = 100) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM bot_logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def clear_logs() -> None:
    """Delete all activity log entries."""
    with _connect() as conn:
        conn.execute("DELETE FROM bot_logs")


# ── Bot status ────────────────────────────────────────────────────────────────

# Columns that callers are permitted to update via update_bot_status().
# This allow-list prevents SQL injection through dynamic key construction.
_BOT_STATUS_ALLOWED_COLS: frozenset[str] = frozenset({
    "running", "symbol", "last_signal", "signal_hint", "waiting_for",
    "long_ready", "short_ready", "last_price",
    "equity", "open_trades", "total_trades", "win_trades",
})


def update_bot_status(**kwargs) -> None:
    unknown = set(kwargs) - _BOT_STATUS_ALLOWED_COLS
    if unknown:
        raise ValueError(f"Unknown bot_status column(s): {unknown}")
    kwargs["updated_at"] = datetime.now(timezone.utc).isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [1]
    with _connect() as conn:
        conn.execute(
            f"UPDATE bot_status SET {set_clause} WHERE id = ?", values
        )


def get_bot_status() -> dict:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM bot_status WHERE id = 1").fetchone()
    return dict(row) if row else {}
