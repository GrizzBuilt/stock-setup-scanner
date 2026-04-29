# File: db.py
# Purpose: SQLite helpers for Stock Setup Scanner
# Includes watchlist storage and one active position record.

import sqlite3

DB_NAME = "scanner.db"


def get_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL UNIQUE
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS active_position (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            symbol TEXT NOT NULL,
            entry REAL NOT NULL,
            shares REAL NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    default_symbols = [
        "NVDA",
        "TSLA",
        "AMD",
        "AMZN",
        "META",
        "PLTR",
        "SMCI",
        "AAPL",
        "MSFT",
        "GOOGL",
        "GOOG",
        "NFLX",
    ]

    for symbol in default_symbols:
        conn.execute(
            "INSERT OR IGNORE INTO watchlist (symbol) VALUES (?)",
            (symbol,)
        )

    conn.commit()
    conn.close()


def get_watchlist():
    conn = get_connection()
    rows = conn.execute(
        "SELECT symbol FROM watchlist ORDER BY symbol"
    ).fetchall()
    conn.close()

    return [row["symbol"] for row in rows]


def add_symbol(symbol):
    symbol = symbol.upper().strip()

    if not symbol:
        return

    conn = get_connection()
    conn.execute(
        "INSERT OR IGNORE INTO watchlist (symbol) VALUES (?)",
        (symbol,)
    )
    conn.commit()
    conn.close()


def remove_symbol(symbol):
    symbol = symbol.upper().strip()

    conn = get_connection()
    conn.execute(
        "DELETE FROM watchlist WHERE symbol = ?",
        (symbol,)
    )
    conn.commit()
    conn.close()


def get_active_position():
    conn = get_connection()
    row = conn.execute("""
        SELECT symbol, entry, shares
        FROM active_position
        WHERE id = 1
    """).fetchone()
    conn.close()

    if not row:
        return None

    return {
        "symbol": row["symbol"],
        "entry": row["entry"],
        "shares": row["shares"],
    }


def set_active_position(symbol, entry, shares):
    symbol = symbol.upper().strip()

    if not symbol:
        return

    entry = float(entry)
    shares = float(shares)

    if entry <= 0 or shares <= 0:
        return

    conn = get_connection()
    conn.execute("""
        INSERT INTO active_position (id, symbol, entry, shares, updated_at)
        VALUES (1, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            symbol = excluded.symbol,
            entry = excluded.entry,
            shares = excluded.shares,
            updated_at = CURRENT_TIMESTAMP
    """, (symbol, entry, shares))

    conn.commit()
    conn.close()


def clear_active_position():
    conn = get_connection()
    conn.execute("DELETE FROM active_position WHERE id = 1")
    conn.commit()
    conn.close()
