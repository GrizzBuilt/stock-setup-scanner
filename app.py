from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, render_template, request, redirect, url_for

import yfinance as yf

from db import init_db, get_watchlist, add_symbol, remove_symbol

app = Flask(__name__)


def empty_stock(symbol, message="No data"):
    return {
        "symbol": symbol,
        "price": "—",
        "open": "—",
        "high": "—",
        "low": "—",
        "percent": 0,
        "range_size": 0,
        "previous_high": "—",
        "previous_low": "—",
        "status": "No Trade",
        "entry": "—",
        "stop": "—",
        "target": "—",
        "error": message,
    }


def get_stock_data(symbol):
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period="5d")

        if data.empty or len(data) < 2:
            return empty_stock(symbol, "Not enough data")

        row = data.iloc[-1]
        previous_row = data.iloc[-2]

        previous_high = float(previous_row["High"])
        previous_low = float(previous_row["Low"])

        price = float(row["Close"])
        open_price = float(row["Open"])
        high = float(row["High"])
        low = float(row["Low"])

        change = price - open_price
        percent = (change / open_price) * 100 if open_price else 0

        range_size = high - low
        near_prev_high = price >= previous_high * 0.995
        ready_to_trigger = price >= previous_high * 0.9975
        distance_to_breakout = ((previous_high - price) / previous_high) * 100

        status = "No Trade"
        entry = None
        stop = None
        target = None

        if percent > 0.5 and near_prev_high:
            status = "Breakout Watch"
            entry = previous_high
            stop = previous_low
            target = previous_high + range_size

        elif percent > 0 and price > open_price:
            status = "Pullback"
            entry = price
            stop = low
            target = price + 3

        elif abs(percent) < 0.5:
            status = "Consolidation"
            entry = high
            stop = low
            target = high + 3

        return {
            "symbol": symbol,
            "price": round(price, 2),
            "open": round(open_price, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "percent": round(percent, 2),
            "range_size": round(range_size, 2),
            "previous_high": round(previous_high, 2),
            "previous_low": round(previous_low, 2),
            "status": status,
            "entry": round(entry, 2) if entry else "—",
            "stop": round(stop, 2) if stop else "—",
            "target": round(target, 2) if target else "—",
            "distance_to_breakout": round(distance_to_breakout, 2),
            "ready_to_trigger": ready_to_trigger,
            "error": None,
        }

    except Exception as e:
        return empty_stock(symbol, str(e))


@app.route("/")
def home():
    symbols = get_watchlist()
    stocks = [get_stock_data(symbol) for symbol in symbols]

    priority = {
        "Breakout Watch": 1,
        "Pullback": 2,
        "Consolidation": 3,
        "No Trade": 4,
    }

    stocks.sort(key=lambda stock: priority.get(stock.get("status"), 99))

    focus_count = sum(
        1 for stock in stocks
        if stock.get("status") in ["Breakout Watch", "Pullback"]
    )

    return render_template(
        "index.html",
        stocks=stocks,
        focus_count=focus_count,
        last_updated=datetime.now(ZoneInfo("America/New_York")).strftime("%I:%M:%S %p"),
    )


@app.route("/add", methods=["POST"])
def add():
    symbol = request.form.get("symbol", "")
    add_symbol(symbol)
    return redirect(url_for("home"))


@app.route("/remove/<symbol>", methods=["POST"])
def remove(symbol):
    remove_symbol(symbol)
    return redirect(url_for("home"))


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)