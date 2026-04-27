from flask import Flask, render_template

import yfinance as yf

from db import init_db, get_watchlist

app = Flask(__name__)


def get_stock_data(symbol):
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period="1d")

        if data.empty:
            return {"symbol": symbol, "error": "No data"}

        row = data.iloc[-1]

        price = float(row["Close"])
        open_price = float(row["Open"])
        high = float(row["High"])
        low = float(row["Low"])

        change = price - open_price
        percent = (change / open_price) * 100 if open_price else 0

        range_size = high - low
        near_high = price >= high - (range_size * 0.2)

        status = "No Trade"
        entry = None
        stop = None
        target = None

        if percent > 1 and near_high:
            status = "Breakout Watch"
            entry = high
            stop = price - 2
            target = price + 5

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
            "status": status,
            "entry": round(entry, 2) if entry else "—",
            "stop": round(stop, 2) if stop else "—",
            "target": round(target, 2) if target else "—",
            "error": None,
        }

    except Exception as e:
        return {"symbol": symbol, "error": str(e)}


@app.route("/")
def home():
    symbols = get_watchlist()
    stocks = [get_stock_data(symbol) for symbol in symbols]

    return render_template("index.html", stocks=stocks)

    for stock in stocks:
        if stock.get("error"):
            rows += f"<li>{stock['symbol']} - ERROR: {stock['error']}</li>"
        else:
            rows += f"""
            <li>
                <strong>{stock['symbol']}</strong> |
                Price: {stock['price']} |
                Change: {stock['percent']}% |
                Status: {stock['status']} |
                Entry: {stock['entry']} |
                Stop: {stock['stop']} |
                Target: {stock['target']}
            </li>
            """

    return f"""
    <h1>Stock Setup Scanner</h1>
    <p>Live Data:</p>
    <ul>
        {rows}
    </ul>
    """


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)