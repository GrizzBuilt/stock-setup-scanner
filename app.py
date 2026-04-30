# File: app.py
# Purpose: Flask stock scanner with setup scoring, Sniper Mode,
# entry-gap chase protection, and SQLite Active Position Mode.

from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, render_template, request, redirect, url_for

import yfinance as yf

from db import (
    init_db,
    get_watchlist,
    add_symbol,
    remove_symbol,
    get_active_position,
    set_active_position,
    clear_active_position,
)

app = Flask(__name__)


def add_empty_position_fields(stock):
    stock.update(
        {
            "is_active_position": False,
            "active_position_warning": "",
            "actual_entry": "",
            "actual_shares": "",
            "actual_pl": 0,
            "actual_pl_per_share": 0,
            "actual_entry_gap": 0,
            "protect_level": "",
            "quick_profit_level": "",
            "goal_profit_level": "",
            "cut_level": "",
            "position_verdict": "",
            "position_management": "",
        }
    )

    return stock


def empty_stock(symbol, message="No data"):
    stock = {
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
        "entry_gap": 0,
        "entry_gap_pct": 0,
        "entry_warning": "NO DATA",
        "focus_rank": "",
        "focus_label": "",
        "reason": message,
    }

    return add_empty_position_fields(stock)


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


def get_confidence(decision, action, sniper, score, rr, status, entry_gap):
    if (
        decision == "TRADE"
        and action == "READY NOW"
        and sniper == "YES"
        and score >= 8
        and rr >= 1.5
        and entry_gap <= 0.50
    ):
        return "HIGH"

    if (
        decision == "TRADE"
        and action == "READY NOW"
        and score >= 7
        and rr >= 1.5
        and entry_gap <= 1.00
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


def build_management(
    decision,
    action,
    sniper,
    confidence,
    trade_style,
    entry,
    stop,
    target,
    entry_gap,
    entry_warning,
):
    if action == "WAIT FOR RESET":
        return (
            "Price is too far above the scanner entry. Do not chase. "
            "Wait for price to reset closer to entry or form a fresh setup."
        )

    if decision == "TRADE" and action == "READY NOW" and trade_style == "SNIPER":
        return (
            "SNIPER setup. You can take the trade if it matches your plan. "
            "At +$0.50, protect entry. At +$1.00, either take profit or trail tight. "
            "Do not let a clean green trade turn red."
        )

    if decision == "TRADE" and action == "READY NOW" and trade_style == "QUICK TRADE":
        return (
            "Valid trade, but entry is not perfect. Treat it as a quick trade. "
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


def build_verdict(decision, action, sniper, confidence, trade_style, status, entry_warning):
    if action == "WAIT FOR RESET":
        return "DO NOT CHASE — WAIT FOR RESET"

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


def build_reason(
    status,
    decision,
    action,
    rr,
    score,
    dist,
    blockers,
    confidence,
    trade_style,
    entry_gap,
    entry_warning,
):
    blocker_text = ""

    if blockers:
        blocker_text = " Blocker: " + "; ".join(blockers) + "."

    prefix = f"{trade_style}. Confidence: {confidence}. Entry: {entry_warning}."

    if action == "WAIT FOR RESET":
        return (
            f"{prefix} Price is ${round(entry_gap, 2)} above scanner entry. "
            f"Wait for a reset instead of chasing.{blocker_text}"
        )

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


def get_entry_warning(entry_gap):
    if entry_gap <= 0.25:
        return "IDEAL ENTRY ZONE"

    if entry_gap <= 0.50:
        return "STILL ACCEPTABLE"

    if entry_gap <= 1.00:
        return "LATE ENTRY / QUICK TRADE ONLY"

    return "DO NOT CHASE"


def build_position_verdict(
    current_price,
    actual_entry,
    shares,
    scanner_entry,
    sniper,
    trade_style,
):
    pl_per_share = current_price - actual_entry
    total_pl = pl_per_share * shares
    actual_entry_gap = actual_entry - scanner_entry

    protect_level = actual_entry
    quick_profit_level = actual_entry + 0.50
    goal_profit_level = actual_entry + 1.00
    cut_level = actual_entry - 0.50

    if actual_entry_gap > 1.00:
        entry_gap_warning = (
            f"Your entry is ${round(actual_entry_gap, 2)} above scanner entry. "
            "This is late. Manage tight."
        )
    elif actual_entry_gap > 0.50:
        entry_gap_warning = (
            f"Your entry is ${round(actual_entry_gap, 2)} above scanner entry. "
            "Quick trade only."
        )
    elif actual_entry_gap >= 0:
        entry_gap_warning = "Your entry is close to scanner entry."
    else:
        entry_gap_warning = "Your entry is better than scanner entry."

    if pl_per_share >= 1.00 and sniper == "YES":
        verdict = "TRAIL WITH CONFIDENCE — LOCK PROFIT"
        management = (
            "You are up more than $1.00/share on a Sniper setup. "
            "Trail it, but do not let it come back under your protect level."
        )

    elif pl_per_share >= 0.50:
        verdict = "TAKE PROFIT OK — PROTECT GREEN"
        management = (
            "You are in the quick profit zone. Taking profit is valid. "
            "If you hold, protect entry and do not let the trade turn red."
        )

    elif pl_per_share > 0:
        verdict = "GREEN — PROTECT ENTRY"
        management = (
            "You are green but not at the quick profit goal yet. "
            "Let it try, but protect your actual entry."
        )

    elif -0.50 < pl_per_share <= 0:
        verdict = "NEEDS RECLAIM — WATCH TIGHT"
        management = (
            "Price is below your actual entry. It needs to reclaim your entry soon. "
            "Do not average down and do not let a small red trade become a big one."
        )

    elif -1.00 < pl_per_share <= -0.50:
        verdict = "BELOW CUT ZONE — NEEDS FAST RECLAIM"
        management = (
            "Price is below your planned cut zone. If it cannot reclaim quickly, "
            "cut the trade and protect the account."
        )

    else:
        verdict = "CUT NOW — LOSS TOO BIG"
        management = (
            "The trade has moved too far against your actual entry. "
            "Protect the account. Do not hope and do not average down."
        )

    return {
        "actual_pl": round(total_pl, 2),
        "actual_pl_per_share": round(pl_per_share, 2),
        "actual_entry_gap": round(actual_entry_gap, 2),
        "protect_level": round(protect_level, 2),
        "quick_profit_level": round(quick_profit_level, 2),
        "goal_profit_level": round(goal_profit_level, 2),
        "cut_level": round(cut_level, 2),
        "position_verdict": verdict,
        "position_management": f"{entry_gap_warning} {management}",
    }


def apply_active_position_to_stocks(stocks, active_position):
    for stock in stocks:
        add_empty_position_fields(stock)

    if not active_position:
        return stocks

    active_symbol = active_position["symbol"].upper().strip()
    actual_entry = float(active_position["entry"])
    shares = float(active_position["shares"])

    found_active_row = False

    for stock in stocks:
        symbol = stock.get("symbol", "").upper().strip()

        if symbol != active_symbol:
            stock["active_position_warning"] = (
                f"ACTIVE POSITION IS {active_symbol} — THIS ROW IS {symbol}"
            )
            continue

        found_active_row = True
        stock["is_active_position"] = True
        stock["actual_entry"] = round(actual_entry, 2)
        stock["actual_shares"] = shares
        stock["active_position_warning"] = "ACTIVE POSITION — MANAGE THIS TICKER"

        try:
            current_price = float(stock.get("price", 0))
            scanner_entry = float(stock.get("entry", 0))
        except Exception:
            stock["position_verdict"] = "ACTIVE POSITION — PRICE DATA ERROR"
            stock["position_management"] = "Could not calculate active P/L."
            continue

        position_data = build_position_verdict(
            current_price=current_price,
            actual_entry=actual_entry,
            shares=shares,
            scanner_entry=scanner_entry,
            sniper=stock.get("sniper", "NO"),
            trade_style=stock.get("trade_style", "AVOID"),
        )

        stock.update(position_data)

    if not found_active_row:
        for stock in stocks:
            stock["active_position_warning"] = (
                f"ACTIVE POSITION IS {active_symbol}, BUT IT IS NOT IN THIS WATCHLIST"
            )

    return stocks


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

        dist = ((price - prev_high) / prev_high) * 100

        status = "Not Near Setup"
        decision = "WATCH"
        action = "WAIT"

        entry = prev_high
        stop = prev_low
        target = prev_high + range_size

        # Setup classification only. Final trade permission happens later.
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

        risk = entry - stop
        reward = target - entry
        rr = reward / risk if risk > 0 else 0

        entry_gap = price - entry
        entry_gap_pct = (entry_gap / entry) * 100 if entry else 0
        entry_warning = get_entry_warning(entry_gap)

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

        if entry_gap > 0.50:
            score -= 1

        if entry_gap > 1.00:
            score -= 2

        if status == "Extended":
            score -= 2

        if status in ["Breakout Triggered", "Pullback"] and rr < 1.5:
            score -= 2

        if risk <= 0:
            score -= 2

        if status == "Not Near Setup":
            score -= 1

        score = max(0, min(score, 10))

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

        if status in ["Breakout Triggered", "Pullback"] and entry_gap > 0.50:
            blockers.append("late entry; price is more than $0.50 above scanner entry")

        if status in ["Breakout Triggered", "Pullback"] and entry_gap > 1.00:
            blockers.append("do not chase; price is more than $1.00 above scanner entry")

        # Final trade decision. READY NOW can only happen here.
        if status == "Breakout Triggered":
            if risk > 0 and reward > 0 and rr >= 1.5 and score >= 7:
                if entry_gap <= 1.00:
                    decision = "TRADE"
                    action = "READY NOW"
                else:
                    decision = "WATCH"
                    action = "WAIT FOR RESET"
            else:
                decision = "PASS"
                action = "PASS"

        elif status == "Pullback":
            if risk > 0 and reward > 0 and rr >= 1.5 and score >= 7:
                if entry_gap <= 1.00:
                    decision = "TRADE"
                    action = "READY NOW"
                else:
                    decision = "WATCH"
                    action = "WAIT FOR RESET"
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
            and entry_gap <= 0.50
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
            and entry_gap <= 0.50
        )

        if clean_breakout_sniper or clean_pullback_sniper:
            sniper = "YES"

        grade = get_grade(score)

        confidence = get_confidence(
            decision=decision,
            action=action,
            sniper=sniper,
            score=score,
            rr=rr,
            status=status,
            entry_gap=entry_gap,
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
            entry_gap=entry_gap,
            entry_warning=entry_warning,
        )

        verdict = build_verdict(
            decision=decision,
            action=action,
            sniper=sniper,
            confidence=confidence,
            trade_style=trade_style,
            status=status,
            entry_warning=entry_warning,
        )

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
            entry_gap=entry_gap,
            entry_warning=entry_warning,
        )

        stock = {
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
            "entry_gap": round(entry_gap, 2),
            "entry_gap_pct": round(entry_gap_pct, 2),
            "entry_warning": entry_warning,
            "focus_rank": "",
            "focus_label": "",
            "reason": reason,
        }

        return add_empty_position_fields(stock)

    except Exception as e:
        return empty_stock(symbol, str(e))


def is_focus_candidate(stock):
    return (
        stock.get("decision") == "TRADE"
        and stock.get("action") == "READY NOW"
        and stock.get("score", 0) >= 7
        and stock.get("rr", 0) >= 1.5
        and stock.get("status") != "Extended"
        and stock.get("entry_gap", 999) <= 1.00
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
        "WAIT FOR RESET": 5,
        "TOO LATE / EXTENDED": 6,
        "PASS": 7,
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
    active_bonus = 0 if stock.get("is_active_position") else 1
    focus_bonus = 0 if is_focus_candidate(stock) else 1

    return (
        active_bonus,
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
    active_position = get_active_position()

    stocks = [get_stock_data(s) for s in symbols]
    stocks = apply_active_position_to_stocks(stocks, active_position)

    stocks.sort(key=scanner_sort_key)
    stocks = add_focus_labels(stocks)

    return render_template(
        "index.html",
        stocks=stocks,
        active_position=active_position,
        last_updated=datetime.now(ZoneInfo("America/New_York")).strftime("%I:%M:%S %p"),
    )


@app.route("/position", methods=["POST"])
def set_position():
    symbol = request.form.get("position_symbol", "").upper().strip()
    entry_raw = request.form.get("position_entry", "").strip()
    shares_raw = request.form.get("position_shares", "").strip()

    try:
        entry = float(entry_raw)
        shares = float(shares_raw)

        if symbol and entry > 0 and shares > 0:
            set_active_position(symbol, entry, shares)

    except ValueError:
        pass

    return redirect(url_for("home"))


@app.route("/position/clear", methods=["POST"])
def clear_position():
    clear_active_position()
    return redirect(url_for("home"))


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