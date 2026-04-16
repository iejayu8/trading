"""
database.py – SQLite persistence layer.

Tables
──────
trades       – all executed (simulated or live) trades
bot_logs     – timestamped bot activity log
bot_status   – singleton row tracking current bot state
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.getenv("TRADING_DB_PATH", str(Path(__file__).parent / "trading_bot.db")))


@contextlib.contextmanager
def _db():
    """Open a SQLite connection, yield it inside a transaction, then close it.

    Uses WAL journal mode and a 10-second busy-timeout so concurrent bot
    threads do not immediately raise ``OperationalError: database is locked``.
    The connection is always closed on exit — unlike the bare
    ``with sqlite3.connect(...) as conn:`` pattern which only
    commits/rolls back but never closes the underlying file handle.
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if they don't exist."""
    with _db() as conn:
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
                symbol        TEXT    PRIMARY KEY,
                running       INTEGER NOT NULL DEFAULT 0,
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

            CREATE TABLE IF NOT EXISTS copy_trading_config (
                id         INTEGER PRIMARY KEY DEFAULT 1,
                enabled    INTEGER NOT NULL DEFAULT 0,
                trader_id  TEXT    NOT NULL DEFAULT '',
                updated_at TEXT    NOT NULL
            );
            """
        )

        # ── Lightweight migrations for existing DBs ──────────────────────────
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(bot_status)").fetchall()
        }
        # Migrate old singleton schema (id INTEGER PRIMARY KEY) → symbol-keyed schema.
        if "id" in cols and "symbol" not in cols:
            # Rename old table, recreate with new schema, drop old.
            conn.executescript(
                """
                ALTER TABLE bot_status RENAME TO bot_status_old;
                CREATE TABLE IF NOT EXISTS bot_status (
                    symbol        TEXT    PRIMARY KEY,
                    running       INTEGER NOT NULL DEFAULT 0,
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
                DROP TABLE bot_status_old;
                """
            )
        # Add missing columns for old DBs already on symbol-keyed schema.
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(bot_status)").fetchall()
        }
        for col, defn in [
            ("signal_hint",  "TEXT    NOT NULL DEFAULT 'WAIT'"),
            ("waiting_for",  "TEXT    NOT NULL DEFAULT 'Collecting candles'"),
            ("long_ready",   "INTEGER NOT NULL DEFAULT 0"),
            ("short_ready",  "INTEGER NOT NULL DEFAULT 0"),
        ]:
            if col not in cols:
                conn.execute(f"ALTER TABLE bot_status ADD COLUMN {col} {defn}")

        # Seed the copy_trading_config singleton row if it doesn't exist yet.
        conn.execute(
            "INSERT OR IGNORE INTO copy_trading_config (id, enabled, trader_id, updated_at)"
            " VALUES (1, 0, '', datetime('now'))"
        )


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
    with _db() as conn:
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
    with _db() as conn:
        conn.execute(
            """
            UPDATE trades
            SET exit_price = ?, pnl = ?, status = 'CLOSED', closed_at = ?
            WHERE id = ?
            """,
            (exit_price, pnl, now, trade_id),
        )


def get_trade_by_id(trade_id: int) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
    return dict(row) if row else None


def get_open_trades(symbol: str | None = None) -> list[dict]:
    with _db() as conn:
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
    with _db() as conn:
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


def get_trade_stats(symbol: str | None = None) -> dict:
    with _db() as conn:
        if symbol:
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
                WHERE status = 'CLOSED' AND symbol = ?
                """,
                (symbol,),
            ).fetchone()
        else:
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
    with _db() as conn:
        conn.execute(
            "INSERT INTO bot_logs (level, message, ts) VALUES (?, ?, ?)",
            (level, message, now),
        )


def get_logs(limit: int = 100) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM bot_logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def clear_logs() -> None:
    """Delete all activity log entries."""
    with _db() as conn:
        conn.execute("DELETE FROM bot_logs")


# ── Bot status ────────────────────────────────────────────────────────────────

# Columns that callers are permitted to update via update_bot_status().
# This allow-list prevents SQL injection through dynamic key construction.
_BOT_STATUS_ALLOWED_COLS: frozenset[str] = frozenset({
    "running", "last_signal", "signal_hint", "waiting_for",
    "long_ready", "short_ready", "last_price",
    "equity", "open_trades", "total_trades", "win_trades",
})


def _ensure_symbol_status(conn: sqlite3.Connection, symbol: str) -> None:
    """Insert a status row for *symbol* if one doesn't exist yet."""
    conn.execute(
        "INSERT OR IGNORE INTO bot_status (symbol, updated_at) VALUES (?, datetime('now'))",
        (symbol,),
    )


def update_bot_status(symbol: str, **kwargs) -> None:
    unknown = set(kwargs) - _BOT_STATUS_ALLOWED_COLS
    if unknown:
        raise ValueError(f"Unknown bot_status column(s): {unknown}")
    kwargs["updated_at"] = datetime.now(timezone.utc).isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [symbol]
    with _db() as conn:
        _ensure_symbol_status(conn, symbol)
        conn.execute(
            f"UPDATE bot_status SET {set_clause} WHERE symbol = ?", values
        )


def get_bot_status(symbol: str) -> dict:
    with _db() as conn:
        _ensure_symbol_status(conn, symbol)
        row = conn.execute(
            "SELECT * FROM bot_status WHERE symbol = ?", (symbol,)
        ).fetchone()
    return dict(row) if row else {}


def get_all_bot_status() -> list[dict]:
    """Return status rows for all symbols."""
    with _db() as conn:
        rows = conn.execute("SELECT * FROM bot_status ORDER BY symbol").fetchall()
    return [dict(r) for r in rows]


def reset_running_flags() -> None:
    """Set running=0 for every symbol.

    Called once on server startup so stale running=1 entries left by a
    previous (crashed or killed) process don't confuse the frontend.
    No bot threads survive a server restart, so the DB must reflect that.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _db() as conn:
        conn.execute(
            "UPDATE bot_status SET running = 0, updated_at = ?",
            (now,),
        )


def reset_database() -> None:
    """Wipe all trade history, logs, and bot status rows.

    Deletes every row from the ``trades``, ``bot_logs``, and ``bot_status``
    tables.  The schema is kept intact so the application can immediately
    write new data without needing a restart.  Compatible with both the
    standalone desktop app (DB_PATH resolved from env or default location
    next to the backend) and the Home Assistant add-on (DB_PATH set via
    the ``TRADING_DB_PATH`` environment variable).
    """
    with _db() as conn:
        conn.executescript(
            """
            DELETE FROM trades;
            DELETE FROM bot_logs;
            DELETE FROM bot_status;
            """
        )


# ── Copy trading configuration ────────────────────────────────────────────────

def get_copy_trading_config() -> dict:
    """Return the current copy trading configuration as ``{enabled, trader_id}``."""
    with _db() as conn:
        row = conn.execute(
            "SELECT enabled, trader_id FROM copy_trading_config WHERE id = 1"
        ).fetchone()
    if row:
        return {"enabled": bool(row["enabled"]), "trader_id": row["trader_id"]}
    return {"enabled": False, "trader_id": ""}


def set_copy_trading_config(enabled: bool, trader_id: str) -> None:
    """Persist copy trading settings.  Creates the singleton row if absent."""
    now = datetime.now(timezone.utc).isoformat()
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO copy_trading_config (id, enabled, trader_id, updated_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                enabled    = excluded.enabled,
                trader_id  = excluded.trader_id,
                updated_at = excluded.updated_at
            """,
            (1 if enabled else 0, trader_id, now),
        )
