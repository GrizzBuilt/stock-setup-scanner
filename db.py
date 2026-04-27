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

    default_symbols = ["NVDA", "TSLA", "AMD", "AMZN", "META", "PLTR", "SMCI"]

    for symbol in default_symbols:
        conn.execute(
            "INSERT OR IGNORE INTO watchlist (symbol) VALUES (?)",
            (symbol,)
        )

    conn.commit()
    conn.close()


def get_watchlist():
    conn = get_connection()
    rows = conn.execute("SELECT symbol FROM watchlist ORDER BY symbol").fetchall()
    conn.close()

    return [row["symbol"] for row in rows]