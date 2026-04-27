from flask import Flask

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

        if percent > 1 and near_high:
            status = "Breakout Watch"
        elif percent > 0 and price > open_price:
            status = "Pullback"
        elif abs(percent) < 0.5:
            status = "Consolidation"
        else:
            status = "No Trade"

        return {
            "symbol": symbol,
            "price": round(price, 2),
            "open": round(open_price, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "percent": round(percent, 2),
            "status": status,
            "error": None,
        }

    except Exception as e:
        return {"symbol": symbol, "error": str(e)}


@app.route("/")
def home():
    symbols = get_watchlist()
    stocks = [get_stock_data(symbol) for symbol in symbols]

    rows = ""

    for stock in stocks:
        if stock.get("error"):
            rows += f"<li>{stock['symbol']} - ERROR: {stock['error']}</li>"
        else:
            rows += f"""
            <li>
                {stock['symbol']} |
                Price: {stock['price']} |
                Change: {stock['percent']}% |
                Status: {stock['status']}
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