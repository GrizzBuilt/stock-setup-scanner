# File: app.py
# Purpose: Main Flask app + upgraded trading logic engine
# Changes:
# - Added Sniper Mode
# - Added Action field
# - Added Risk/Reward calculation
# - Added Score + Grade system
# - Improved breakout + pullback classification
# - Added reasoning text for decisions
# - Added Focus Rank so the best 1–2 actionable setups rise to the top
# - Fixed decision override so WATCH setups are not automatically forced to PASS
# - Updated pullback wording:
#   - "WAIT FOR PULLBACK" is now "WAIT FOR BOUNCE"
#   - Reason text now says "Waiting for bounce confirmation"
# - Allowed clean pullback setups to become Sniper YES when confirmed by logic
# - Cleaned up early action labels:
#   - Early classification no longer sets READY NOW
#   - READY NOW can only happen after final risk/reward + score filters pass
# - Added clearer non-setup wording:
#   - Status: Not Near Setup
#   - Action: WAIT

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
        if decision == "TRADE" and action == "READY NOW":
            return f"Breakout triggered. RR {round(rr, 2)}. Score {score}/10. Clean trade candidate."
        return f"Breakout triggered, but not clean enough yet. RR {round(rr, 2)}. Score {score}/10.{blocker_text}"

    if status == "Breakout Watch":
        return f"Near breakout level. Watching for breakout confirmation. RR {round(rr, 2)}. Score {score}/10.{blocker_text}"

    if status == "Pullback":
        if decision == "TRADE" and action == "READY NOW":
            return f"Pullback bounce confirmed. RR {round(rr, 2)}. Score {score}/10. Clean trade candidate."
        return f"Pullback setup forming. Waiting for bounce confirmation. RR {round(rr, 2)}. Score {score}/10.{blocker_text}"

    if status == "Not Near Setup":
        return f"Not close enough to a clean setup yet. Distance from previous high is {round(dist, 2)}%. RR {round(rr, 2)}. Score {score}/10."

    return f"No clean setup.{blocker_text}"


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

        # --- DISTANCE FROM PREVIOUS HIGH ---
        # Positive = price is above yesterday's high.
        # Negative = price is still below yesterday's high.
        dist = ((price - prev_high) / prev_high) * 100

        status = "Not Near Setup"
        decision = "WATCH"
        action = "WAIT"

        entry = prev_high
        stop = prev_low
        target = prev_high + range_size

        # --- SETUP CLASSIFICATION ---
        # Important:
        # This section only classifies the setup.
        # It does NOT give the final trading green light.
        # READY NOW only happens later after score + risk/reward filters pass.
        if -0.5 <= dist <= 0:
            status = "Breakout Watch"
            decision = "WATCH"
            action = "WATCH FOR BREAK"

        elif 0 < dist <= 1:
            status = "Breakout Triggered"
            decision = "WATCH"
            action = "CHECK SETUP"

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

        # --- PENALTIES ---
        if status == "Extended":
            score -= 2

        if status in ["Breakout Triggered", "Pullback"] and rr < 1.5:
            score -= 2

        if risk <= 0:
            score -= 2

        if status == "Not Near Setup":
            score -= 1

        score = max(0, min(score, 10))

        # --- BLOCKERS ---
        blockers = []

        if status == "Extended":
            blockers.append("too extended")

        if risk <= 0:
            blockers.append("invalid risk")

        if reward <= 0:
            blockers.append("no upside target")

        if status in ["Breakout Triggered", "Pullback"] and rr < 1.5:
            blockers.append("risk/reward below 1.5")

        if status in ["Breakout Triggered", "Pullback"] and score < 7:
            blockers.append("score below trade quality")

        # --- FINAL TRADE DECISION ---
        # This is the only section allowed to set READY NOW.
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

        elif status == "Not Near Setup":
            decision = "WATCH"
            action = "WAIT"

        else:
            decision = "PASS"
            action = "PASS"

        # --- SNIPER MODE ---
        sniper = "NO"

        clean_breakout_sniper = (
            decision == "TRADE"
            and action == "READY NOW"
            and score >= 7
            and status == "Breakout Triggered"
            and risk > 0
            and reward > 0
            and rr >= 1.5
            and 0 < dist <= 0.5
        )

        clean_pullback_sniper = (
            decision == "TRADE"
            and action == "READY NOW"
            and score >= 7
            and status == "Pullback"
            and risk > 0
            and reward > 0
            and rr >= 1.5
            and price > open_price
            and price > stop
        )

        if clean_breakout_sniper or clean_pullback_sniper:
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
        stock.get("decision") == "TRADE"
        and stock.get("sniper") == "YES"
        and stock.get("score", 0) >= 7
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


def action_priority(stock):
    priorities = {
        "READY NOW": 0,
        "WATCH FOR BREAK": 1,
        "WAIT FOR BOUNCE": 2,
        "CHECK SETUP": 3,
        "WAIT": 4,
        "TOO LATE / EXTENDED": 5,
        "PASS": 6,
    }

    return priorities.get(stock.get("action"), 9)


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
        action_priority(stock),
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
    symbols = get_watchlist()
    stocks = [get_stock_data(s) for s in symbols]

    stocks.sort(key=scanner_sort_key)
    stocks = add_focus_labels(stocks)

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
