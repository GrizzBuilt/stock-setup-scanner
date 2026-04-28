# File: app.py
# Purpose: Main Flask app + upgraded trading logic engine
# Changes:
# - Added Sniper Mode
# - Added Action field
# - Added Risk/Reward calculation
# - Added Score + Grade system
# - Improved breakout + pullback classification
# - Added reasoning text for decisions

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
        "status": "No Trade",
        "decision": "PASS",
        "action": "PASS",
        "score": 0,
        "grade": "F",
        "sniper": "NO",
        "rr": "—",
        "distance": 0,
        "reason": message,
    }


def get_stock_data(symbol):
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period="5d")

        if data.empty or len(data) < 2:
            return empty_stock(symbol, "Not enough data")

        row = data.iloc[-1]
        prev = data.iloc[-2]

        price = float(row["Close"])
        open_price = float(row["Open"])
        high = float(row["High"])
        low = float(row["Low"])

        prev_high = float(prev["High"])
        prev_low = float(prev["Low"])

        percent = ((price - open_price) / open_price) * 100 if open_price else 0
        range_size = high - low

        # --- DISTANCE FROM BREAKOUT ---
        dist = ((price - prev_high) / prev_high) * 100

        status = "No Trade"
        decision = "PASS"
        action = "PASS"

        entry = prev_high
        stop = prev_low
        target = prev_high + range_size

        # --- CLASSIFICATION ---
        if -0.5 <= dist <= 0:
            status = "Breakout Watch"
            decision = "WATCH"
            action = "WATCH FOR BREAK"

        elif 0 < dist <= 1:
            status = "Breakout Triggered"
            decision = "TRADE"
            action = "READY NOW"

        elif dist > 1:
            status = "Extended"
            decision = "PASS"
            action = "TOO LATE / EXTENDED"

        elif percent > 0 and price > open_price and range_size >= 2:
            status = "Pullback"
            decision = "WATCH"
            action = "WAIT FOR PULLBACK"
            entry = price
            stop = low
            target = price + 3

        # --- RISK / REWARD ---
        risk = entry - stop
        reward = target - entry
        rr = reward / risk if risk > 0 else 0

        # --- SCORE ---
        score = 0

        if status in ["Breakout Watch", "Breakout Triggered", "Pullback"]:
            score += 2

        if abs(dist) <= 0.25:
            score += 2
        elif abs(dist) <= 0.5:
            score += 1

        if range_size >= 3:
            score += 2
        elif range_size >= 2:
            score += 1

        if 0.5 <= percent <= 3:
            score += 1

        if rr >= 1.5:
            score += 1
        if rr >= 2:
            score += 1

        if risk > 0:
            score += 1
        if reward > 0:
            score += 1

        # penalties
        if status == "Extended":
            score -= 2

        if rr < 1.5:
            score -= 2

        score = max(0, min(score, 10))

        # --- SNIPER MODE ---
        sniper = "NO"

        if (
            decision == "TRADE"
            and score >= 7
            and status in ["Breakout Triggered", "Pullback"]
            and status != "Extended"
            and risk > 0
            and reward > 0
            and rr >= 1.5
            and abs(dist) <= 0.5
        ):
            sniper = "YES"

        # --- FINAL DECISION OVERRIDE ---
        if rr < 1.5 or risk <= 0:
            decision = "PASS"
            sniper = "NO"
            action = "PASS"

        # --- GRADE ---
        if score >= 9 and sniper == "YES":
            grade = "A+"
        elif score == 8 and sniper == "YES":
            grade = "A"
        elif score == 7:
            grade = "B"
        elif score == 6:
            grade = "C"
        else:
            grade = "F"

        # --- REASON TEXT ---
        if status == "Extended":
            reason = f"Price is {round(dist,2)}% above previous high. Extended."
        elif status == "Breakout Triggered":
            reason = f"Breakout triggered. RR {round(rr,2)}. Score {score}/10."
        elif status == "Breakout Watch":
            reason = f"Near breakout level. Watching for move."
        elif status == "Pullback":
            reason = f"Pullback setup forming. Waiting confirmation."
        else:
            reason = "No clean setup."

        return {
            "symbol": symbol,
            "price": round(price, 2),
            "percent": round(percent, 2),
            "status": status,
            "decision": decision,
            "action": action,
            "score": score,
            "grade": grade,
            "sniper": sniper,
            "rr": round(rr, 2) if rr else 0,
            "distance": round(dist, 2),
            "entry": round(entry, 2),
            "stop": round(stop, 2),
            "target": round(target, 2),
            "reason": reason,
        }

    except Exception as e:
        return empty_stock(symbol, str(e))


@app.route("/")
def home():
    symbols = get_watchlist()
    stocks = [get_stock_data(s) for s in symbols]

    stocks.sort(key=lambda x: (-x["score"], x["decision"]))

    return render_template(
        "index.html",
        stocks=stocks,
        last_updated=datetime.now(ZoneInfo("America/New_York")).strftime("%I:%M:%S %p"),
    )


@app.route("/add", methods=["POST"])
def add():
    add_symbol(request.form.get("symbol", ""))
    return redirect(url_for("home"))


@app.route("/remove/<symbol>", methods=["POST"])
def remove(symbol):
    remove_symbol(symbol)
    return redirect(url_for("home"))


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)