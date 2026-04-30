# File: app.py
# Purpose: Main Flask app + upgraded trading logic engine
# Changes:
# - Added Quick / Swing scanner mode using ?mode=quick or ?mode=swing
# - Quick mode keeps the current breakout/sniper logic
# - Swing mode scans for pullbacks in confirmed uptrends
# - Added swing-specific statuses and reason text
# - Keeps existing scoring, sorting, focus labels, and UI data structure

from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, render_template, request, redirect, url_for

import yfinance as yf

from db import init_db, get_watchlist, add_symbol, remove_symbol

app = Flask(__name__)


VALID_MODES = ["quick", "swing"]


def empty_stock(symbol, message="No data"):
    return {
        "symbol": symbol,
        "price": "—",
        "percent": 0,
        "status": "No Trade",
        "decision": "PASS",
        "action": "PASS",
        "score": 0,
        "grade": "F",
        "sniper": "NO",
        "rr": 0,
        "distance": 0,
        "entry": "—",
        "stop": "—",
        "target": "—",
        "focus_rank": "",
        "focus_label": "",
        "reason": message,
    }


def get_grade(score, sniper):
    if score >= 9 and sniper == "YES":
        return "A+"
    if score >= 8 and sniper == "YES":
        return "A"
    if score >= 7:
        return "B"
    if score >= 6:
        return "C"
    return "F"


def build_reason(status, decision, action, rr, score, dist, blockers):
    blocker_text = ""

    if blockers:
        blocker_text = " Blocker: " + "; ".join(blockers) + "."

    if status == "Extended":
        return f"Price is {round(dist, 2)}% above previous high. Extended.{blocker_text}"

    if status == "Breakout Triggered":
        if decision == "TRADE":
            return f"Breakout triggered. RR {round(rr, 2)}. Score {score}/10. Clean trade candidate."
        return f"Breakout triggered, but not clean enough yet. RR {round(rr, 2)}. Score {score}/10.{blocker_text}"

    if status == "Breakout Watch":
        return f"Near breakout level. Watching for breakout confirmation. RR {round(rr, 2)}. Score {score}/10.{blocker_text}"

    if status == "Pullback":
        if action == "READY NOW":
            return f"Pullback bounce confirmed. RR {round(rr, 2)}. Score {score}/10. Clean trade candidate."
        return f"Pullback setup forming. Waiting for bounce confirmation. RR {round(rr, 2)}. Score {score}/10.{blocker_text}"

    if status == "Swing Pullback":
        return f"Swing pullback in an uptrend. Waiting for breakout confirmation. RR {round(rr, 2)}. Score {score}/10.{blocker_text}"

    if status == "Trend Strength":
        return f"Stock is in an uptrend, but not pulled back enough for a clean swing entry yet. RR {round(rr, 2)}. Score {score}/10.{blocker_text}"

    if status == "No Trend":
        return f"No confirmed swing uptrend yet.{blocker_text}"

    return f"No clean setup.{blocker_text}"


def get_stock_data(symbol, mode="quick"):
    try:
        ticker = yf.Ticker(symbol)

        # Pull enough data for both quick and swing mode.
        data = ticker.history(period="3mo")

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

        # Positive = price is above yesterday's high.
        # Negative = price is still below yesterday's high.
        dist = ((price - prev_high) / prev_high) * 100 if prev_high else 0

        status = "No Trade"
        decision = "PASS"
        action = "PASS"

        entry = prev_high
        stop = prev_low
        target = prev_high + range_size

        # ============================================================
        # QUICK MODE
        # Fast breakout / pullback scanner for short trades.
        # ============================================================

        if mode == "quick":
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
                action = "WAIT FOR BOUNCE"
                entry = price
                stop = low
                target = price + 3

        # ============================================================
        # SWING MODE
        # Slower scanner for multi-day setups.
        # Looks for pullbacks inside confirmed uptrends.
        # ============================================================

        elif mode == "swing":
            if len(data) < 50:
                return empty_stock(symbol, "Not enough trend data for swing mode")

            sma20 = data["Close"].tail(20).mean()
            sma50 = data["Close"].tail(50).mean()

            recent_20 = data.tail(20)
            recent_high = float(recent_20["High"].max())
            recent_low = float(recent_20["Low"].min())

            trend_up = price > sma20 and sma20 > sma50
            pullback = price < recent_high * 0.97 and price > sma20
            extended = (price - sma20) / sma20 > 0.08 if sma20 else False

            entry = recent_high
            stop = recent_low
            target = entry + ((entry - stop) * 2)

            if trend_up and pullback and not extended:
                status = "Swing Pullback"
                decision = "WATCH"
                action = "WAIT FOR BREAK"

            elif trend_up and not pullback and not extended:
                status = "Trend Strength"
                decision = "WATCH"
                action = "WAIT FOR PULLBACK"

            elif extended:
                status = "Extended"
                decision = "PASS"
                action = "TOO LATE / EXTENDED"

            else:
                status = "No Trend"
                decision = "PASS"
                action = "PASS"

        # --- RISK / REWARD ---
        risk = entry - stop
        reward = target - entry
        rr = reward / risk if risk > 0 else 0

        # --- SCORE ---
        score = 0

        if status in [
            "Breakout Watch",
            "Breakout Triggered",
            "Pullback",
            "Swing Pullback",
            "Trend Strength",
        ]:
            score += 2

        if mode == "quick":
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

        elif mode == "swing":
            if status == "Swing Pullback":
                score += 3

            if status == "Trend Strength":
                score += 1

            if -3 <= percent <= 2:
                score += 1

            if price > stop:
                score += 1

        if rr >= 1.5:
            score += 1

        if rr >= 2:
            score += 1

        if risk > 0:
            score += 1

        if reward > 0:
            score += 1

        # --- PENALTIES ---
        if status == "Extended":
            score -= 2

        if status in ["Breakout Triggered", "Pullback"] and rr < 1.5:
            score -= 2

        if mode == "swing" and status == "Swing Pullback" and rr < 1.5:
            score -= 2

        if risk <= 0:
            score -= 2

        score = max(0, min(score, 10))

        # --- BLOCKERS ---
        blockers = []

        if status == "Extended":
            blockers.append("too extended")

        if risk <= 0:
            blockers.append("invalid risk")

        if reward <= 0:
            blockers.append("no upside target")

        if status in ["Breakout Triggered", "Pullback", "Swing Pullback"] and rr < 1.5:
            blockers.append("risk/reward below 1.5")

        if mode == "quick" and status in ["Breakout Triggered", "Pullback"] and score < 7:
            blockers.append("score below trade quality")

        if mode == "swing" and status == "Swing Pullback" and score < 6:
            blockers.append("swing setup not strong enough yet")

        # --- FINAL TRADE DECISION ---
        if mode == "quick":
            if status == "Breakout Triggered":
                if risk > 0 and reward > 0 and rr >= 1.5 and score >= 7:
                    decision = "TRADE"
                    action = "READY NOW"
                else:
                    decision = "PASS"
                    action = "PASS"

            elif status == "Pullback":
                if risk > 0 and reward > 0 and rr >= 1.5 and score >= 7:
                    decision = "TRADE"
                    action = "READY NOW"
                else:
                    decision = "WATCH"
                    action = "WAIT FOR BOUNCE"

            elif status == "Breakout Watch":
                decision = "WATCH"
                action = "WATCH FOR BREAK"

            elif status == "Extended":
                decision = "PASS"
                action = "TOO LATE / EXTENDED"

            else:
                decision = "PASS"
                action = "PASS"

        elif mode == "swing":
            if status == "Swing Pullback":
                decision = "WATCH"
                action = "WAIT FOR BREAK"

            elif status == "Trend Strength":
                decision = "WATCH"
                action = "WAIT FOR PULLBACK"

            elif status == "Extended":
                decision = "PASS"
                action = "TOO LATE / EXTENDED"

            else:
                decision = "PASS"
                action = "PASS"

        # --- SNIPER MODE ---
        sniper = "NO"

        clean_breakout_sniper = (
            mode == "quick"
            and decision == "TRADE"
            and score >= 7
            and status == "Breakout Triggered"
            and risk > 0
            and reward > 0
            and rr >= 1.5
            and 0 < dist <= 0.5
        )

        clean_pullback_sniper = (
            mode == "quick"
            and decision == "TRADE"
            and score >= 7
            and status == "Pullback"
            and risk > 0
            and reward > 0
            and rr >= 1.5
            and price > open_price
            and price > stop
        )

        clean_swing_watch = (
            mode == "swing"
            and status == "Swing Pullback"
            and score >= 6
            and risk > 0
            and reward > 0
            and rr >= 1.5
        )

        if clean_breakout_sniper or clean_pullback_sniper or clean_swing_watch:
            sniper = "YES"

        # --- GRADE ---
        grade = get_grade(score, sniper)

        # --- REASON TEXT ---
        reason = build_reason(
            status=status,
            decision=decision,
            action=action,
            rr=rr,
            score=score,
            dist=dist,
            blockers=blockers,
        )

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
            "focus_rank": "",
            "focus_label": "",
            "reason": reason,
        }

    except Exception as e:
        return empty_stock(symbol, str(e))


def is_focus_candidate(stock):
    return (
        stock.get("sniper") == "YES"
        and stock.get("score", 0) >= 6
        and stock.get("rr", 0) >= 1.5
        and stock.get("status") != "Extended"
    )


def decision_priority(stock):
    priorities = {
        "TRADE": 0,
        "WATCH": 1,
        "PASS": 2,
    }

    return priorities.get(stock.get("decision"), 9)


def grade_priority(stock):
    priorities = {
        "A+": 0,
        "A": 1,
        "B": 2,
        "C": 3,
        "F": 4,
    }

    return priorities.get(stock.get("grade"), 9)


def scanner_sort_key(stock):
    focus_bonus = 0 if is_focus_candidate(stock) else 1

    return (
        focus_bonus,
        decision_priority(stock),
        grade_priority(stock),
        -stock.get("score", 0),
        -stock.get("rr", 0),
        abs(stock.get("distance", 999)),
        stock.get("symbol", ""),
    )


def add_focus_labels(stocks):
    focus_candidates = [stock for stock in stocks if is_focus_candidate(stock)]

    for index, stock in enumerate(focus_candidates[:2], start=1):
        stock["focus_rank"] = index

        if index == 1:
            stock["focus_label"] = "BEST SETUP"
        else:
            stock["focus_label"] = "BACKUP SETUP"

    return stocks


@app.route("/")
def home():
    mode = request.args.get("mode", "quick").lower()

    if mode not in VALID_MODES:
        mode = "quick"

    symbols = get_watchlist()
    stocks = [get_stock_data(s, mode) for s in symbols]

    stocks.sort(key=scanner_sort_key)
    stocks = add_focus_labels(stocks)

    return render_template(
        "index.html",
        stocks=stocks,
        mode=mode,
        last_updated=datetime.now(ZoneInfo("America/New_York")).strftime("%I:%M:%S %p"),
    )


@app.route("/add", methods=["POST"])
def add():
    mode = request.form.get("mode", "quick").lower()

    if mode not in VALID_MODES:
        mode = "quick"

    add_symbol(request.form.get("symbol", ""))
    return redirect(url_for("home", mode=mode))


@app.route("/remove/<symbol>", methods=["POST"])
def remove(symbol):
    mode = request.form.get("mode", "quick").lower()

    if mode not in VALID_MODES:
        mode = "quick"

    remove_symbol(symbol)
    return redirect(url_for("home", mode=mode))


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)