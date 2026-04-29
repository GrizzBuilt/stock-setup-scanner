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
# - Added confidence + trade style fields to make decisions faster:
#   - Confidence: HIGH / MEDIUM / LOW / NONE
#   - Trade Style: SNIPER / QUICK TRADE / WATCH ONLY / AVOID
#   - Management: plain-English in-trade guidance
# - Added Verdict field:
#   - One-line plain-English decision helper for faster judgment

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
        "confidence": "NONE",
        "trade_style": "AVOID",
        "management": "No trade. Wait for a cleaner setup.",
        "verdict": "NO TRADE — PASS",
        "rr": 0,
        "distance": 0,
        "entry": "—",
        "stop": "—",
        "target": "—",
        "focus_rank": "",
        "focus_label": "",
        "reason": message,
    }


def get_grade(score):
    if score >= 9:
        return "A+"
    if score >= 8:
        return "A"
    if score >= 7:
        return "B"
    if score >= 6:
        return "C"
    return "F"


def get_confidence(decision, action, sniper, score, rr, status):
    if (
        decision == "TRADE"
        and action == "READY NOW"
        and sniper == "YES"
        and score >= 8
        and rr >= 1.5
    ):
        return "HIGH"

    if (
        decision == "TRADE"
        and action == "READY NOW"
        and score >= 7
        and rr >= 1.5
    ):
        return "MEDIUM"

    if decision == "WATCH" and status in ["Breakout Watch", "Pullback"]:
        return "LOW"

    return "NONE"


def get_trade_style(decision, action, sniper, confidence):
    if decision == "TRADE" and action == "READY NOW" and sniper == "YES":
        return "SNIPER"

    if decision == "TRADE" and action == "READY NOW" and confidence == "MEDIUM":
        return "QUICK TRADE"

    if decision == "WATCH":
        return "WATCH ONLY"

    return "AVOID"


def build_management(decision, action, sniper, confidence, trade_style, entry, stop, target):
    if decision == "TRADE" and action == "READY NOW" and trade_style == "SNIPER":
        return (
            "SNIPER setup. You can take the trade if it matches your plan. "
            "At +$0.50, protect entry. At +$1.00, either take profit or trail tight. "
            "Do not let a clean green trade turn red."
        )

    if decision == "TRADE" and action == "READY NOW" and trade_style == "QUICK TRADE":
        return (
            "Valid trade, but not A+ Sniper. Treat it as a quick trade. "
            "Take +$0.50 to +$1.00 if offered, protect entry fast, and do not expect a runner."
        )

    if action == "WATCH FOR BREAK":
        return (
            "Watch only. Do not enter yet. Wait for price to break the level and hold. "
            "No confirmation, no trade."
        )

    if action == "WAIT FOR BOUNCE":
        return (
            "Watch only. The pullback is happening, but the bounce is not confirmed. "
            "Wait for price to stop falling and reclaim strength."
        )

    if action == "TOO LATE / EXTENDED":
        return "Too extended. Do not chase. Wait for a reset or pullback."

    if action == "WAIT":
        return "Not near a clean setup. Keep it on watch, but do not force a trade."

    return "No trade. Wait for a cleaner setup."


def build_verdict(decision, action, sniper, confidence, trade_style, status):
    if decision == "TRADE" and action == "READY NOW" and sniper == "YES":
        return "A+ SETUP — TRAIL WITH CONFIDENCE"

    if decision == "TRADE" and action == "READY NOW" and trade_style == "QUICK TRADE":
        return "VALID QUICK TRADE — TAKE $0.50–$1.00 IF OFFERED"

    if action == "WATCH FOR BREAK":
        return "WATCH ONLY — WAIT FOR BREAK"

    if action == "WAIT FOR BOUNCE":
        return "WATCH ONLY — WAIT FOR BOUNCE"

    if action == "TOO LATE / EXTENDED":
        return "DO NOT CHASE — WAIT FOR RESET"

    if status == "Not Near Setup" or action == "WAIT":
        return "NOT NEAR SETUP — WAIT"

    return "NO TRADE — PASS"


def build_reason(status, decision, action, rr, score, dist, blockers, confidence, trade_style):
    blocker_text = ""

    if blockers:
        blocker_text = " Blocker: " + "; ".join(blockers) + "."

    prefix = f"{trade_style}. Confidence: {confidence}."

    if status == "Extended":
        return f"{prefix} Price is {round(dist, 2)}% above previous high. Extended.{blocker_text}"

    if status == "Breakout Triggered":
        if decision == "TRADE" and action == "READY NOW":
            return (
                f"{prefix} Breakout triggered. RR {round(rr, 2)}. "
                f"Score {score}/10. Trade is valid now, but manage based on trade style."
            )
        return (
            f"{prefix} Breakout triggered, but not clean enough yet. "
            f"RR {round(rr, 2)}. Score {score}/10.{blocker_text}"
        )

    if status == "Breakout Watch":
        return (
            f"{prefix} Near breakout level. Watching for breakout confirmation. "
            f"RR {round(rr, 2)}. Score {score}/10.{blocker_text}"
        )

    if status == "Pullback":
        if decision == "TRADE" and action == "READY NOW":
            return (
                f"{prefix} Pullback bounce confirmed. RR {round(rr, 2)}. "
                f"Score {score}/10. Trade is valid now, but manage based on trade style."
            )
        return (
            f"{prefix} Pullback setup forming. Waiting for bounce confirmation. "
            f"RR {round(rr, 2)}. Score {score}/10.{blocker_text}"
        )

    if status == "Not Near Setup":
        return (
            f"{prefix} Not close enough to a clean setup yet. "
            f"Distance from previous high is {round(dist, 2)}%. "
            f"RR {round(rr, 2)}. Score {score}/10."
        )

    return f"{prefix} No clean setup.{blocker_text}"


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
        # Sniper is reserved for the cleanest A+ style setups.
        # A setup can still be TRADE / READY NOW without being Sniper.
        sniper = "NO"

        clean_breakout_sniper = (
            decision == "TRADE"
            and action == "READY NOW"
            and score >= 8
            and status == "Breakout Triggered"
            and risk > 0
            and reward > 0
            and rr >= 1.5
            and 0 < dist <= 0.5
        )

        clean_pullback_sniper = (
            decision == "TRADE"
            and action == "READY NOW"
            and score >= 8
            and status == "Pullback"
            and risk > 0
            and reward > 0
            and rr >= 1.5
            and price > open_price
            and price > stop
        )

        if clean_breakout_sniper or clean_pullback_sniper:
            sniper = "YES"

        # --- CONFIDENCE + MANAGEMENT ---
        grade = get_grade(score)

        confidence = get_confidence(
            decision=decision,
            action=action,
            sniper=sniper,
            score=score,
            rr=rr,
            status=status,
        )

        trade_style = get_trade_style(
            decision=decision,
            action=action,
            sniper=sniper,
            confidence=confidence,
        )

        management = build_management(
            decision=decision,
            action=action,
            sniper=sniper,
            confidence=confidence,
            trade_style=trade_style,
            entry=entry,
            stop=stop,
            target=target,
        )

        verdict = build_verdict(
            decision=decision,
            action=action,
            sniper=sniper,
            confidence=confidence,
            trade_style=trade_style,
            status=status,
        )

        # --- REASON TEXT ---
        reason = build_reason(
            status=status,
            decision=decision,
            action=action,
            rr=rr,
            score=score,
            dist=dist,
            blockers=blockers,
            confidence=confidence,
            trade_style=trade_style,
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
            "confidence": confidence,
            "trade_style": trade_style,
            "management": management,
            "verdict": verdict,
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
        and stock.get("action") == "READY NOW"
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


def confidence_priority(stock):
    priorities = {
        "HIGH": 0,
        "MEDIUM": 1,
        "LOW": 2,
        "NONE": 3,
    }

    return priorities.get(stock.get("confidence"), 9)


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
        confidence_priority(stock),
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
