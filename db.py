# File: db.py
# Purpose: SQLite helpers for Stock Setup Scanner
# Includes watchlist storage and active trade records.

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
            capital REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    active_position_columns = [
        row["name"]
        for row in conn.execute("PRAGMA table_info(active_position)").fetchall()
    ]

    if "capital" not in active_position_columns:
        conn.execute("ALTER TABLE active_position ADD COLUMN capital REAL")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS active_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            entry REAL NOT NULL,
            capital REAL NOT NULL,
            shares REAL NOT NULL,
            entry_date TEXT DEFAULT CURRENT_DATE,
            trade_type TEXT DEFAULT 'swing',
            status TEXT DEFAULT 'active',
            exit_price REAL,
            exit_date TEXT,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    legacy_position = conn.execute("""
        SELECT symbol, entry, shares, capital
        FROM active_position
        WHERE id = 1
    """).fetchone()

    if legacy_position:
        existing_trade = conn.execute("""
            SELECT id
            FROM active_trades
            WHERE symbol = ? AND status = 'active'
        """, (legacy_position["symbol"],)).fetchone()

        if not existing_trade:
            legacy_capital = legacy_position["capital"]

            if not legacy_capital:
                legacy_capital = legacy_position["entry"] * legacy_position["shares"]

            conn.execute("""
                INSERT INTO active_trades (
                    symbol, entry, capital, shares, trade_type, status
                )
                VALUES (?, ?, ?, ?, 'swing', 'active')
            """, (
                legacy_position["symbol"],
                legacy_position["entry"],
                legacy_capital,
                legacy_position["shares"],
            ))

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


def normalize_position_size(entry, shares=None, capital=None):
    entry = float(entry)
    shares = float(shares) if shares not in [None, ""] else 0
    capital = float(capital) if capital not in [None, ""] else 0

    if entry <= 0:
        return None

    if shares <= 0 and capital > 0:
        shares = capital / entry
    elif capital <= 0 and shares > 0:
        capital = shares * entry

    if shares <= 0 or capital <= 0:
        return None

    return shares, capital


def row_to_active_position(row):
    return {
        "id": row["id"],
        "symbol": row["symbol"],
        "entry": row["entry"],
        "actual_entry": row["entry"],
        "capital": row["capital"],
        "capital_in": row["capital"],
        "shares": row["shares"],
        "entry_date": row["entry_date"],
        "trade_type": row["trade_type"],
        "status": row["status"],
        "exit_price": row["exit_price"],
        "exit_date": row["exit_date"],
        "notes": row["notes"],
    }


def get_active_positions():
    conn = get_connection()
    rows = conn.execute("""
        SELECT
            id,
            symbol,
            entry,
            capital,
            shares,
            entry_date,
            trade_type,
            status,
            exit_price,
            exit_date,
            notes
        FROM active_trades
        WHERE status = 'active'
        ORDER BY symbol
    """).fetchall()
    conn.close()

    return [row_to_active_position(row) for row in rows]


def get_active_position():
    active_positions = get_active_positions()
    return active_positions[0] if active_positions else None


def set_active_position(
    symbol,
    entry,
    shares=None,
    capital=None,
    trade_type="swing",
    notes="",
):
    symbol = symbol.upper().strip()
    trade_type = (trade_type or "swing").strip().lower()
    notes = (notes or "").strip()

    if not symbol:
        return

    normalized_size = normalize_position_size(entry, shares=shares, capital=capital)

    if not normalized_size:
        return

    shares, capital = normalized_size
    entry = float(entry)

    conn = get_connection()
    active_trade = conn.execute("""
        SELECT id
        FROM active_trades
        WHERE symbol = ? AND status = 'active'
    """, (symbol,)).fetchone()

    if active_trade:
        conn.execute("""
            UPDATE active_trades
            SET
                entry = ?,
                shares = ?,
                capital = ?,
                trade_type = ?,
                notes = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (entry, shares, capital, trade_type, notes, active_trade["id"]))
    else:
        conn.execute("""
            INSERT INTO active_trades (
                symbol, entry, shares, capital, trade_type, notes, status
            )
            VALUES (?, ?, ?, ?, ?, ?, 'active')
        """, (symbol, entry, shares, capital, trade_type, notes))

    conn.commit()
    conn.close()


def clear_active_position():
    conn = get_connection()
    conn.execute("DELETE FROM active_position WHERE id = 1")
    conn.execute("""
        UPDATE active_trades
        SET status = 'closed', updated_at = CURRENT_TIMESTAMP
        WHERE status = 'active'
    """)
    conn.commit()
    conn.close()


def exit_active_position(symbol, exit_price=None):
    symbol = symbol.upper().strip()
    exit_price = float(exit_price) if exit_price not in [None, ""] else None

    conn = get_connection()
    conn.execute("""
        UPDATE active_trades
        SET
            status = 'exited',
            exit_price = ?,
            exit_date = CURRENT_DATE,
            updated_at = CURRENT_TIMESTAMP
        WHERE symbol = ? AND status = 'active'
    """, (exit_price, symbol))
    conn.commit()
    conn.close()
