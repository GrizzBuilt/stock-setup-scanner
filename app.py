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
        "distance_to_breakout": 999,
        "status": "No Trade",
        "decision": "PASS",
        "score": 0,
        "entry": "—",
        "stop": "—",
        "target": "—",
        "ready_to_trigger": False,
        "error": message,
    }


def score_setup(status, distance_to_breakout, range_size, percent):
    score = 0

    if status == "Breakout Triggered":
        score += 4
    elif status == "Breakout Watch":
        score += 3
    elif status == "Pullback":
        score += 2
    elif status == "Extended":
        score += 0
    else:
        score += 0

    abs_distance = abs(distance_to_breakout)

    if abs_distance <= 0.25:
        score += 3
    elif abs_distance <= 0.5:
        score += 2
    elif abs_distance <= 1:
        score += 1

    if range_size >= 5:
        score += 2
    elif range_size >= 3:
        score += 1.5
    elif range_size >= 2:
        score += 1

    if 0.5 <= percent <= 3:
        score += 1
    elif 0 < percent < 0.5:
        score += 0.5
    elif percent > 3:
        score += 0.25

    if status == "Extended":
        score = min(score, 2)

    if status == "No Trade":
        score = min(score, 3)

    return min(round(score), 10)


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

        distance_to_breakout = ((price - previous_high) / previous_high) * 100

        status = "No Trade"
        decision = "PASS"
        entry = "—"
        stop = "—"
        target = "—"

        if -0.5 <= distance_to_breakout <= 0:
            status = "Breakout Watch"
            decision = "WATCH"
            entry = previous_high
            stop = previous_low
            target = previous_high + range_size

        elif 0 < distance_to_breakout <= 1:
            status = "Breakout Triggered"
            decision = "TRADE"
            entry = previous_high
            stop = previous_low
            target = previous_high + range_size

        elif distance_to_breakout > 1:
            status = "Extended"
            decision = "PASS"

        elif percent > 0 and price > open_price and range_size >= 2:
            status = "Pullback"
            decision = "WATCH"
            entry = price
            stop = low
            target = price + 3

        score = score_setup(
            status=status,
            distance_to_breakout=distance_to_breakout,
            range_size=range_size,
            percent=percent,
        )

        ready_to_trigger = (
            status in ["Breakout Watch", "Breakout Triggered"]
            and abs(distance_to_breakout) <= 0.25
        )

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
            "distance_to_breakout": round(distance_to_breakout, 2),
            "status": status,
            "decision": decision,
            "score": score,
            "entry": round(entry, 2) if isinstance(entry, float) else entry,
            "stop": round(stop, 2) if isinstance(stop, float) else stop,
            "target": round(target, 2) if isinstance(target, float) else target,
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
        "Breakout Triggered": 1,
        "Breakout Watch": 2,
        "Pullback": 3,
        "Extended": 4,
        "No Trade": 5,
    }

    stocks.sort(
        key=lambda stock: (
            priority.get(stock.get("status"), 99),
            -stock.get("score", 0),
        )
    )

    focus_count = sum(
        1 for stock in stocks
        if stock.get("decision") in ["TRADE", "WATCH"]
    )

    trade_count = sum(
        1 for stock in stocks
        if stock.get("decision") == "TRADE"
    )

    return render_template(
        "index.html",
        stocks=stocks,
        focus_count=focus_count,
        trade_count=trade_count,
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