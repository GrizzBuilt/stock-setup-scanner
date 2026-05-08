# File: app.py
# Purpose: Flask stock scanner with setup scoring, Sniper Mode,
# entry-gap chase protection, SQLite Active Position Mode,
# optional Quick / Swing scanner modes,
# earnings awareness, volume confirmation, and stronger active trade protection.

import os

from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request, redirect, url_for

import yfinance as yf

from db import (
    init_db,
    get_watchlist,
    add_symbol,
    remove_symbol,
    get_active_positions,
    set_active_position,
    clear_active_position,
    exit_active_position,
)

app = Flask(__name__)

VALID_MODES = ["quick", "swing"]
SWING_REWARD_MULTIPLE = 1.75
SWING_MAX_TARGET_PCT = 10
SWING_MAX_ENTRY_ABOVE_PRICE_PCT = 3
SWING_MAX_PRICE_PAST_ENTRY_PCT = 2
SWING_BREAKOUT_CLOSE_PCT = 3


def normalize_mode(mode):
    mode = (mode or "quick").lower().strip()

    if mode not in VALID_MODES:
        return "quick"

    return mode


def build_hold_plan(mode, status, action, decision):
    mode = normalize_mode(mode)

    if mode == "quick":
        return {
            "hold_plan": "Quick trade only",
            "hold_days": "Minutes to 1 day",
            "review_rule": "Manage fast",
            "exit_rule": "Cut if setup fails",
            "profit_rule": "Take $0.50–$1.00 if offered",
        }

    if status == "Swing Pullback":
        return {
            "hold_plan": "Short swing",
            "hold_days": "2–5 trading days",
            "review_rule": "Review daily. Hold only while price stays above stop and trend holds.",
            "exit_rule": "Exit if price closes below stop, loses the 20-day average, or momentum fails.",
            "profit_rule": "Take profit near target or trail if strength continues.",
        }

    if status == "Swing Base":
        return {
            "hold_plan": "Base breakout swing",
            "hold_days": "3–10 trading days",
            "review_rule": "Review daily. Hold while breakout/base structure stays intact.",
            "exit_rule": "Exit if price breaks back below the base, closes below stop, or volume fades after breakout.",
            "profit_rule": "Take profit near target, or trail under higher lows.",
        }

    if status == "Swing Trend":
        return {
            "hold_plan": "Trend watch",
            "hold_days": "5–15 trading days if trend continues",
            "review_rule": "Review daily. Hold only while price stays above the 20-day average and keeps making higher lows.",
            "exit_rule": "Exit if price loses the 20-day average, breaks trend support, or becomes too extended.",
            "profit_rule": "Trail instead of guessing a top.",
        }

    if status == "Swing Extended":
        return {
            "hold_plan": "Do not chase",
            "hold_days": "No planned hold",
            "review_rule": "Wait for reset.",
            "exit_rule": "Avoid new entry while extended.",
            "profit_rule": "No profit plan because this is not a clean entry.",
        }

    return {
        "hold_plan": "No swing plan",
        "hold_days": "No planned hold",
        "review_rule": "Wait for cleaner structure.",
        "exit_rule": "No trade.",
        "profit_rule": "No target until setup forms.",
    }


def get_earnings_blocker(ticker):
    """
    Best-effort earnings awareness.
    yfinance earnings calendar can vary by symbol/API response, so this is intentionally safe.
    Returns a blocker string if earnings appear to be within the next 2 calendar days.
    """
    try:
        calendar = ticker.calendar

        if calendar is None:
            return ""

        earnings_value = None

        if isinstance(calendar, dict):
            earnings_value = calendar.get("Earnings Date") or calendar.get("EarningsDate")
        else:
            try:
                if "Earnings Date" in calendar.index:
                    earnings_value = calendar.loc["Earnings Date"][0]
            except Exception:
                pass

        if earnings_value is None:
            return ""

        if isinstance(earnings_value, (list, tuple)):
            earnings_value = earnings_value[0]

        if hasattr(earnings_value, "to_pydatetime"):
            earnings_date = earnings_value.to_pydatetime().date()
        elif hasattr(earnings_value, "date"):
            earnings_date = earnings_value.date()
        else:
            return ""

        today = datetime.now(ZoneInfo("America/New_York")).date()
        days_until = (earnings_date - today).days

        if 0 <= days_until <= 2:
            return f"earnings within {days_until} day(s)"

    except Exception:
        return ""

    return ""


def add_empty_position_fields(stock):
    stock.update(
        {
            "is_active_position": False,
            "active_position_warning": "",
            "actual_entry": "",
            "actual_shares": "",
            "capital_in": "",
            "current_value": "",
            "percent_gain": "",
            "actual_pl": 0,
            "actual_pl_per_share": 0,
            "actual_entry_gap": 0,
            "protect_level": "",
            "green_protect": "",
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
        "blockers": [],
        "reason": message,
    }

    stock.update(build_hold_plan("quick", stock["status"], stock["action"], stock["decision"]))

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

    if decision == "WATCH" and status in [
        "Breakout Watch",
        "Pullback",
        "Swing Pullback",
        "Swing Base",
        "Swing Trend",
    ]:
        return "LOW"

    return "NONE"


def get_trade_style(decision, action, sniper, confidence):
    if decision == "TRADE" and action == "READY NOW" and sniper == "YES":
        return "SNIPER"

    if decision == "TRADE" and action == "READY NOW" and confidence == "MEDIUM":
        return "QUICK TRADE"

    if action in ["SWING WATCH", "WAIT FOR SWING BREAK"]:
        return "SWING WATCH"

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

    if action == "WAIT FOR SWING BREAK":
        return (
            "Swing watch only. Do not enter just because it looks good. "
            "Wait for price to break above the swing trigger and hold strength."
        )

    if action == "SWING WATCH":
        return (
            "Swing setup forming. This is not a quick trade signal. "
            "Watch for trend continuation, a clean reclaim, or a break above resistance."
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

    if action == "WATCH LOW PRIORITY":
        return (
            "Near a level, but risk/reward is weak. Keep it low priority and wait "
            "for a cleaner setup."
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

    if action == "WAIT FOR SWING BREAK":
        return "SWING WATCH — WAIT FOR BREAK"

    if action == "SWING WATCH":
        return "SWING SETUP FORMING — WATCH"

    if decision == "TRADE" and action == "READY NOW" and sniper == "YES":
        return "A+ SETUP — TRAIL WITH CONFIDENCE"

    if decision == "TRADE" and action == "READY NOW" and trade_style == "QUICK TRADE":
        return "VALID QUICK TRADE — TAKE $0.50–$1.00 IF OFFERED"

    if action == "WATCH FOR BREAK":
        return "WATCH ONLY — WAIT FOR BREAK"

    if action == "WATCH LOW PRIORITY":
        return "LOW PRIORITY WATCH — WEAK RISK/REWARD"

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

    if status == "Swing Pullback":
        return (
            f"{prefix} Swing pullback inside an uptrend. "
            f"Watch for a break above the swing trigger. "
            f"RR {round(rr, 2)}. Score {score}/10.{blocker_text}"
        )

    if status == "Swing Base":
        return (
            f"{prefix} Swing base forming. Price is holding trend support but has not broken yet. "
            f"RR {round(rr, 2)}. Score {score}/10.{blocker_text}"
        )

    if status == "Swing Trend":
        return (
            f"{prefix} Uptrend is intact, but price has not pulled back enough for a cleaner swing entry. "
            f"RR {round(rr, 2)}. Score {score}/10.{blocker_text}"
        )

    if status == "Swing Extended":
        return (
            f"{prefix} Trend is strong, but price is stretched above the 20-day average. "
            f"Do not chase a swing entry here.{blocker_text}"
        )

    if status == "No Swing Setup":
        return (
            f"{prefix} No clean swing structure yet. "
            f"Wait for trend, pullback, base, or breakout confirmation.{blocker_text}"
        )

    if status == "Extended":
        return f"{prefix} Price is {round(dist, 2)}% above previous high. Extended.{blocker_text}"

    if status == "Breakout Triggered":
        if decision == "TRADE" and action == "READY NOW":
            return (
                f"{prefix} Breakout triggered. RR {round(rr, 2)}. "
                f"Score {score}/10. Trade is valid now, but manage based on trade style."
                f"{blocker_text}"
            )
        return (
            f"{prefix} Breakout triggered, but not clean enough yet. "
            f"RR {round(rr, 2)}. Score {score}/10.{blocker_text}"
        )

    if status == "Breakout Watch":
        if action == "WATCH LOW PRIORITY":
            return (
                f"{prefix} Near breakout, but risk/reward is weak. "
                f"RR {round(rr, 2)} is below 1.5, so this is not top priority. "
                f"Score {score}/10.{blocker_text}"
            )

        return (
            f"{prefix} Near breakout level. Watching for breakout confirmation. "
            f"RR {round(rr, 2)}. Score {score}/10.{blocker_text}"
        )

    if status == "Pullback":
        if decision == "TRADE" and action == "READY NOW":
            return (
                f"{prefix} Pullback bounce confirmed. RR {round(rr, 2)}. "
                f"Score {score}/10. Trade is valid now, but manage based on trade style."
                f"{blocker_text}"
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


def has_blocker(blockers, text):
    text = text.lower()
    return any(text in str(blocker).lower() for blocker in blockers)


def resolve_final_verdict(stock):
    blockers = stock.get("blockers", [])
    rr = float(stock.get("rr") or 0)
    score = int(stock.get("score") or 0)
    status = stock.get("status") or "Setup"
    entry_warning = stock.get("entry_warning") or ""
    blocker_text = ", ".join(blockers)
    has_volume_blocker = has_blocker(blockers, "volume")
    has_invalid_blocker = (
        has_blocker(blockers, "invalid")
        or has_blocker(blockers, "no upside")
        or has_blocker(blockers, "earnings")
    )
    has_chase_blocker = (
        stock.get("status") in ["Extended", "Swing Extended"]
        or stock.get("action") in ["TOO LATE / EXTENDED", "WAIT FOR RESET"]
        or "extended" in entry_warning.lower()
        or has_blocker(blockers, "extended")
        or has_blocker(blockers, "chase")
        or has_blocker(blockers, "late entry")
    )

    if stock.get("position_mode"):
        stock.update(
            {
                "decision": "WATCH",
                "action": "POSITION MODE: MANAGE",
                "sniper": "NO",
                "confidence": "LOW",
                "trade_style": "MANAGE",
                "verdict": "POSITION MODE: MANAGE — DO NOT ADD",
                "management": "Manage the existing position. Do not treat this as a new-entry signal.",
                "reason": "Position mode is active for this ticker.",
            }
        )
        return stock

    if has_volume_blocker:
        stock.update(
            {
                "decision": "WATCH",
                "action": "WAIT FOR VOLUME",
                "sniper": "NO",
                "confidence": "LOW",
                "trade_style": "AVOID",
                "verdict": "WATCH — DO NOT ENTER YET",
                "management": "No new trade. Wait for volume confirmation.",
                "reason": f"{status} is present, but volume is light.",
            }
        )
        return stock

    if has_invalid_blocker:
        stock.update(
            {
                "decision": "WATCH",
                "action": "WAIT FOR CONFIRMATION",
                "sniper": "NO",
                "confidence": "LOW",
                "trade_style": "AVOID",
                "verdict": "WATCH — DO NOT ENTER YET",
                "management": "No new trade. Wait for a cleaner setup.",
                "reason": f"{status} is present, but blocked by: {blocker_text}.",
            }
        )
        return stock

    if rr > 0 and rr < 1.5:
        stock.update(
            {
                "decision": "WATCH",
                "action": "WAIT FOR BETTER R/R",
                "sniper": "NO",
                "confidence": "LOW",
                "trade_style": "AVOID",
                "verdict": "WATCH — RISK/REWARD TOO WEAK",
                "management": "No trade. Wait for a better risk/reward setup.",
                "reason": f"Risk/reward is {round(rr, 2)}, below the minimum acceptable level.",
            }
        )
        return stock

    if has_chase_blocker:
        stock.update(
            {
                "decision": "WATCH",
                "action": "WAIT FOR PULLBACK",
                "sniper": "NO",
                "confidence": "LOW",
                "trade_style": "AVOID",
                "verdict": "WATCH — DO NOT CHASE",
                "management": "No trade. Price is extended. Wait for a cleaner entry.",
                "reason": stock.get("reason") or "Setup exists, but entry is extended.",
            }
        )
        return stock

    if blockers:
        stock.update(
            {
                "decision": "WATCH",
                "action": "WAIT FOR CONFIRMATION",
                "sniper": "NO",
                "confidence": "LOW",
                "trade_style": "AVOID",
                "verdict": "WATCH — DO NOT ENTER YET",
                "management": "No new trade. Wait for a cleaner setup.",
                "reason": f"{status} is present, but blocked by: {blocker_text}.",
            }
        )
        return stock

    if (
        stock.get("decision") == "TRADE"
        and stock.get("action") == "READY NOW"
        and rr >= 1.5
        and score >= 7
        and stock.get("status") not in ["Extended", "Swing Extended"]
    ):
        stock.update(
            {
                "decision": "TRADE",
                "action": "READY NOW",
                "verdict": "TRADE — CLEAN SETUP",
                "trade_style": "SWING" if "Swing" in status else stock.get("trade_style", "QUICK TRADE"),
                "management": "Valid trade setup. Use planned stop and target.",
            }
        )
        return stock

    if stock.get("decision") == "WATCH" and stock.get("action") in [
        "WATCH FOR BREAK",
        "WAIT FOR BOUNCE",
        "WAIT FOR SWING BREAK",
        "SWING WATCH",
    ]:
        stock["sniper"] = "NO"
        return stock

    if score >= 6:
        stock.update(
            {
                "decision": "WATCH",
                "action": "WAIT FOR CLEANER ENTRY",
                "sniper": "NO",
                "confidence": "LOW",
                "trade_style": "WAIT",
                "verdict": "WATCH — NOT READY",
                "management": "Setup is forming, but not clean enough yet.",
            }
        )
        return stock

    stock.update(
        {
            "decision": "PASS",
            "action": "NO TRADE",
            "sniper": "NO",
            "confidence": "NONE",
            "trade_style": "AVOID",
            "verdict": "PASS — NO CLEAN SETUP",
            "management": "No trade. Wait for a better setup.",
        }
    )
    return stock


def get_entry_warning(entry_gap):
    if entry_gap <= 0.25:
        return "IDEAL ENTRY ZONE"

    if entry_gap <= 0.50:
        return "STILL ACCEPTABLE"

    if entry_gap <= 1.00:
        return "LATE ENTRY / QUICK TRADE ONLY"

    return "DO NOT CHASE"


def calculate_atr(data, period=14):
    previous_close = data["Close"].shift(1)
    true_range = data["High"] - data["Low"]
    high_close_range = (data["High"] - previous_close).abs()
    low_close_range = (data["Low"] - previous_close).abs()
    atr = true_range.combine(high_close_range, max).combine(low_close_range, max)

    return float(atr.tail(period).mean())


def pct_above(value, base):
    if not base:
        return 0

    return ((value - base) / base) * 100


def get_swing_confidence(
    decision,
    action,
    score,
    rr,
    trend_up,
    entry_above_price_pct,
    price_past_entry_pct,
    target_above_entry_pct,
    earnings_blocker,
):
    if (
        decision == "TRADE"
        and action == "READY NOW"
        and score >= 8
        and rr >= 1.5
        and trend_up
        and entry_above_price_pct <= SWING_MAX_ENTRY_ABOVE_PRICE_PCT
        and price_past_entry_pct <= SWING_MAX_PRICE_PAST_ENTRY_PCT
        and target_above_entry_pct <= SWING_MAX_TARGET_PCT
        and not earnings_blocker
    ):
        return "HIGH"

    if (
        action == "WAIT FOR SWING BREAK"
        and score >= 7
        and rr >= 1.5
        and trend_up
        and entry_above_price_pct <= SWING_BREAKOUT_CLOSE_PCT
        and target_above_entry_pct <= SWING_MAX_TARGET_PCT
        and not earnings_blocker
    ):
        return "MEDIUM"

    if action in ["WAIT FOR SWING BREAK", "SWING WATCH"] or decision == "WATCH":
        return "LOW"

    return "NONE"


def build_swing_reason(
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
    price,
    entry,
    stop,
    target,
    level_note,
):
    blocker_text = ""

    if blockers:
        blocker_text = " Blocker: " + "; ".join(blockers) + "."

    prefix = f"{trade_style}. Confidence: {confidence}. Entry: {entry_warning}."
    level_text = (
        f" Levels: entry {round(entry, 2)}, stop {round(stop, 2)}, "
        f"target {round(target, 2)}."
    )

    if level_note:
        level_text = f" {level_note}{level_text}"

    if decision == "TRADE" and action == "READY NOW":
        return (
            f"{prefix} Swing setup is actionable now. Current price is "
            f"{round(price, 2)}. RR {round(rr, 2)}. Score {score}/10."
            f"{level_text}{blocker_text}"
        )

    if status == "Swing Pullback":
        return (
            f"{prefix} Swing watch: pullback inside an uptrend. "
            f"Current price is {round(price, 2)}. "
            f"RR {round(rr, 2)}. Score {score}/10.{level_text}{blocker_text}"
        )

    if status == "Swing Base":
        return (
            f"{prefix} Swing watch: base forming near trend support. "
            f"Current price is {round(price, 2)}. "
            f"RR {round(rr, 2)}. Score {score}/10.{level_text}{blocker_text}"
        )

    if status == "Swing Trend":
        return (
            f"{prefix} Uptrend is intact, but price has not pulled back enough "
            f"for a cleaner swing entry. Current price is {round(price, 2)}. "
            f"RR {round(rr, 2)}. Score {score}/10.{level_text}{blocker_text}"
        )

    if status == "Swing Extended":
        return (
            f"{prefix} Trend is strong, but price is stretched above the 20-day "
            f"average. Current price is {round(price, 2)}. "
            f"Do not chase a swing entry here.{level_text}{blocker_text}"
        )

    if status == "No Swing Setup":
        return (
            f"{prefix} No clean swing structure yet. Current price is "
            f"{round(price, 2)}. Wait for trend, pullback, base, or breakout "
            f"confirmation.{level_text}{blocker_text}"
        )

    return build_reason(
        status=status,
        decision="WATCH",
        action="WAIT",
        rr=rr,
        score=score,
        dist=dist,
        blockers=blockers,
        confidence=confidence,
        trade_style=trade_style,
        entry_gap=entry_gap,
        entry_warning=entry_warning,
    )


def build_position_verdict(
    current_price,
    actual_entry,
    shares,
    capital_in,
    scanner_entry,
    sniper,
    trade_style,
):
    pl_per_share = current_price - actual_entry
    total_pl = pl_per_share * shares
    current_value = current_price * shares
    percent_gain = (pl_per_share / actual_entry) * 100 if actual_entry else 0
    actual_entry_gap = actual_entry - scanner_entry

    protect_level = actual_entry
    green_protect = ""
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

    if pl_per_share >= 1.00:
        green_protect = max(actual_entry, actual_entry + (pl_per_share * 0.50))
        management = (
            f"Your position is up ${round(total_pl, 2)} actual / "
            f"${round(pl_per_share, 2)} per share (+{round(percent_gain, 2)}%). "
            f"This is past the goal zone. Take profit, or trail near "
            f"${round(green_protect, 2)} and protect green."
        )
        verdict = "GOAL HIT — TAKE PROFIT OR TRAIL TIGHT"

    elif pl_per_share >= 0.50:
        verdict = "TAKE PROFIT OK — PROTECT GREEN"
        management = (
            "You are in the quick profit zone. Taking profit is valid. "
            "If you hold, protect entry and do not let the trade turn red."
        )

    elif pl_per_share >= 0.25:
        verdict = "SMALL GREEN — MOVE TOWARD BREAKEVEN PROTECTION"
        management = (
            "You are green, but not at the quick profit goal yet. "
            "Start thinking protection. If momentum stalls, do not let it flip red."
        )

    elif pl_per_share > 0:
        verdict = "GREEN — PROTECT ENTRY"
        management = (
            "You are green but barely. Let it try, but protect your actual entry. "
            "Do not let a clean green trade turn red."
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
        "capital_in": round(capital_in, 2),
        "current_value": round(current_value, 2),
        "percent_gain": round(percent_gain, 2),
        "actual_entry_gap": round(actual_entry_gap, 2),
        "protect_level": round(protect_level, 2),
        "green_protect": round(green_protect, 2) if green_protect != "" else "",
        "quick_profit_level": round(quick_profit_level, 2),
        "goal_profit_level": round(goal_profit_level, 2),
        "cut_level": round(cut_level, 2),
        "position_verdict": verdict,
        "position_management": f"{entry_gap_warning} {management}",
    }


def apply_active_position_to_stocks(stocks, active_positions):
    for stock in stocks:
        add_empty_position_fields(stock)

    if not active_positions:
        return stocks

    active_by_symbol = {
        active_position["symbol"].upper().strip(): active_position
        for active_position in active_positions
    }

    for stock in stocks:
        symbol = stock.get("symbol", "").upper().strip()
        active_position = active_by_symbol.get(symbol)

        if not active_position:
            continue

        actual_entry = float(active_position["entry"])
        shares = float(active_position["shares"])
        capital_in = float(active_position.get("capital") or (actual_entry * shares))

        stock["is_active_position"] = True
        stock["active_position_id"] = active_position.get("id", "")
        stock["active_position_trade_type"] = active_position.get("trade_type", "swing")
        stock["active_position_entry_date"] = active_position.get("entry_date", "")
        stock["active_position_notes"] = active_position.get("notes", "")
        stock["actual_entry"] = round(actual_entry, 2)
        stock["actual_shares"] = round(shares, 5)
        stock["capital_in"] = round(capital_in, 2)
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
            capital_in=capital_in,
            scanner_entry=scanner_entry,
            sniper=stock.get("sniper", "NO"),
            trade_style=stock.get("trade_style", "AVOID"),
        )

        stock.update(position_data)

    return stocks


def build_active_trades_summary(stocks, active_positions):
    stocks_by_symbol = {
        stock.get("symbol", "").upper().strip(): stock
        for stock in stocks
    }

    summaries = []

    for active_position in active_positions:
        symbol = active_position["symbol"].upper().strip()
        stock = stocks_by_symbol.get(symbol)

        if stock and stock.get("is_active_position"):
            summaries.append(
                {
                    "symbol": symbol,
                    "entry": stock.get("actual_entry", ""),
                    "current_price": stock.get("price", ""),
                    "capital_in": stock.get("capital_in", ""),
                    "shares": stock.get("actual_shares", ""),
                    "actual_pl": stock.get("actual_pl", ""),
                    "percent_gain": stock.get("percent_gain", ""),
                    "verdict": stock.get("position_verdict", ""),
                    "green_protect": stock.get("green_protect", ""),
                    "trade_type": active_position.get("trade_type", "swing"),
                    "entry_date": active_position.get("entry_date", ""),
                    "notes": active_position.get("notes", ""),
                }
            )
        else:
            summaries.append(
                {
                    "symbol": symbol,
                    "entry": round(float(active_position["entry"]), 2),
                    "current_price": "",
                    "capital_in": round(float(active_position["capital"]), 2),
                    "shares": round(float(active_position["shares"]), 5),
                    "actual_pl": "",
                    "percent_gain": "",
                    "verdict": "ACTIVE POSITION NOT IN WATCHLIST",
                    "green_protect": "",
                    "trade_type": active_position.get("trade_type", "swing"),
                    "entry_date": active_position.get("entry_date", ""),
                    "notes": active_position.get("notes", ""),
                }
            )

    return summaries


def get_quick_stock_data(symbol):
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

        avg_volume = float(data["Volume"].tail(5).mean())
        current_volume = float(row["Volume"])
        volume_ratio = current_volume / avg_volume if avg_volume else 0

        earnings_blocker = get_earnings_blocker(ticker)

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

        # Volume confirmation.
        if volume_ratio >= 1.50:
            score += 1
        elif status in ["Breakout Triggered", "Pullback"] and volume_ratio < 0.80:
            score -= 1

        # Earnings awareness.
        if earnings_blocker:
            score -= 2

        # Entry/chase protection.
        if entry_gap > 0.50:
            score -= 1

        if entry_gap > 1.00:
            score -= 2

        if status == "Breakout Triggered" and dist > 0.50:
            score -= 1

        if status == "Extended":
            score -= 2

        if status in ["Breakout Watch", "Breakout Triggered", "Pullback"] and rr < 1.5:
            score -= 2

        if risk <= 0:
            score -= 2

        if status == "Not Near Setup":
            score -= 1

        score = max(0, min(score, 10))

        blockers = []

        if earnings_blocker:
            blockers.append(earnings_blocker)

        if status == "Extended":
            blockers.append("too extended")

        if risk <= 0:
            blockers.append("invalid risk")

        if reward <= 0:
            blockers.append("no upside target")

        if status in ["Breakout Watch", "Breakout Triggered", "Pullback"] and rr < 1.5:
            blockers.append("risk/reward below 1.5")

        if status == "Breakout Watch" and rr < 1.5:
            blockers.append("near breakout, but risk/reward is weak")

        if status in ["Breakout Triggered", "Pullback"] and score < 7:
            blockers.append("score below trade quality")

        if status in ["Breakout Triggered", "Pullback"] and volume_ratio < 0.80:
            blockers.append("low volume confirmation")

        if status == "Breakout Triggered" and dist > 0.50:
            blockers.append("breakout is already stretched; avoid chasing")

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

            if rr >= 1.5 and score >= 6:
                action = "WATCH FOR BREAK"
            else:
                action = "WATCH LOW PRIORITY"
                score = min(score, 5)

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
            and volume_ratio >= 0.80
            and not earnings_blocker
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
            and volume_ratio >= 0.80
            and not earnings_blocker
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
            "blockers": blockers,
            "reason": reason,
        }

        stock.update(build_hold_plan("quick", status, action, decision))
        stock = resolve_final_verdict(stock)

        return add_empty_position_fields(stock)

    except Exception as e:
        return empty_stock(symbol, str(e))


def get_swing_stock_data(symbol):
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period="3mo")

        if data.empty or len(data) < 50:
            return empty_stock(symbol, "Not enough data for swing mode")

        row = data.iloc[-1]
        prev = data.iloc[-2]

        price = float(row["Close"])
        open_price = float(row["Open"])

        prev_high = float(prev["High"])

        percent = ((price - open_price) / open_price) * 100 if open_price else 0
        dist = ((price - prev_high) / prev_high) * 100 if prev_high else 0

        close_series = data["Close"]
        high_series = data["High"]
        low_series = data["Low"]
        volume_series = data["Volume"]

        sma20 = float(close_series.tail(20).mean())
        sma50 = float(close_series.tail(50).mean())

        recent_20 = data.tail(20)
        recent_10 = data.tail(10)
        recent_5 = data.tail(5)

        recent_high_20 = float(recent_20["High"].max())
        recent_high_10 = float(recent_10["High"].max())
        recent_low_10 = float(recent_10["Low"].min())
        recent_low_5 = float(recent_5["Low"].min())

        avg_volume_20 = float(volume_series.tail(20).mean())
        current_volume = float(row["Volume"])
        volume_ratio = current_volume / avg_volume_20 if avg_volume_20 else 0
        atr = calculate_atr(data)

        earnings_blocker = get_earnings_blocker(ticker)

        trend_up = price > sma20 and sma20 > sma50
        above_sma50 = price > sma50
        holding_sma20 = price >= sma20 * 0.985
        near_sma20 = abs(price - sma20) / sma20 <= 0.03 if sma20 else False

        pullback_from_high_pct = (
            ((recent_high_20 - price) / recent_high_20) * 100
            if recent_high_20
            else 0
        )

        extended_from_sma20_pct = (
            ((price - sma20) / sma20) * 100
            if sma20
            else 0
        )

        range_10 = recent_high_10 - recent_low_10
        avg_range_20 = float((high_series.tail(20) - low_series.tail(20)).mean())
        tightening = range_10 <= avg_range_20 * 4 if avg_range_20 else False

        status = "No Swing Setup"
        decision = "WATCH"
        action = "WAIT"

        blockers = []

        if earnings_blocker:
            blockers.append(earnings_blocker)

        if not trend_up:
            blockers.append("trend not confirmed")

        if not above_sma50:
            blockers.append("below 50-day average")

        if extended_from_sma20_pct > 8:
            blockers.append("too extended above 20-day average")

        if volume_ratio < 0.70:
            blockers.append("volume is light")

        if trend_up and extended_from_sma20_pct > 8:
            status = "Swing Extended"
            decision = "PASS"
            action = "TOO LATE / EXTENDED"

        elif trend_up and 3 <= pullback_from_high_pct <= 10 and holding_sma20:
            status = "Swing Pullback"
            decision = "WATCH"
            action = "WAIT FOR SWING BREAK"

        elif trend_up and near_sma20 and tightening:
            status = "Swing Base"
            decision = "WATCH"
            action = "SWING WATCH"

        elif trend_up:
            status = "Swing Trend"
            decision = "WATCH"
            action = "SWING WATCH"

        else:
            status = "No Swing Setup"
            decision = "WATCH"
            action = "WAIT"

        breakout_trigger = recent_high_10
        breakout_trigger_gap_pct = pct_above(breakout_trigger, price)
        level_note = ""

        if status == "Swing Pullback":
            if 0 <= breakout_trigger_gap_pct <= SWING_BREAKOUT_CLOSE_PCT:
                entry = breakout_trigger
                entry_warning = "SWING BREAKOUT TRIGGER"
                level_note = (
                    f"Swing watch: price below breakout trigger. Entry near "
                    f"{round(entry, 2)} only if price reclaims resistance."
                )
            else:
                entry = price
                entry_warning = "PULLBACK WATCH ZONE"
                level_note = "Swing watch: pullback is forming near current price."

        elif status == "Swing Base":
            if 0 <= breakout_trigger_gap_pct <= SWING_BREAKOUT_CLOSE_PCT:
                entry = breakout_trigger
                entry_warning = "BASE BREAKOUT TRIGGER"
                level_note = (
                    f"Swing watch: base trigger is {round(entry, 2)}. "
                    "Wait for a clean reclaim before treating it as actionable."
                )
            else:
                entry = price
                entry_warning = "BASE WATCH ZONE"
                level_note = "Swing watch: base is forming, but breakout trigger is not close."

        elif status == "Swing Trend":
            entry = price
            entry_warning = "TREND WATCH ZONE"
            level_note = "Swing watch: trend is intact, but no clean trigger is active."

        else:
            entry = price
            entry_warning = "NO SWING ENTRY"
            level_note = "No valid swing entry trigger is active."

        support_stop = min(recent_low_5, float(row["Low"]))
        atr_stop = entry - (2 * atr)
        max_risk_stop = entry * 0.95
        stop = max(support_stop - (0.25 * atr), atr_stop, max_risk_stop)
        target = entry + ((entry - stop) * SWING_REWARD_MULTIPLE)

        risk = entry - stop
        reward = target - entry
        rr = reward / risk if risk > 0 else 0

        entry_gap = price - entry
        entry_gap_pct = (entry_gap / entry) * 100 if entry else 0
        entry_above_price_pct = pct_above(entry, price)
        price_past_entry_pct = pct_above(price, entry)
        target_above_entry_pct = pct_above(target, entry)

        invalid_setup_levels = (
            risk <= 0
            or reward <= 0
            or stop <= 0
            or target <= entry
            or entry <= 0
        )

        if invalid_setup_levels:
            status = "No Swing Setup"
            decision = "PASS"
            action = "PASS"
            entry_warning = "INVALID SETUP LEVELS"
            level_note = "Invalid setup levels."

        score = 0

        if trend_up:
            score += 3

        if price > sma20:
            score += 1

        if sma20 > sma50:
            score += 1

        if status == "Swing Pullback":
            score += 2

        if status == "Swing Base":
            score += 2

        if holding_sma20:
            score += 1

        if tightening:
            score += 1

        if volume_ratio >= 1:
            score += 1

        if volume_ratio >= 1.50:
            score += 1

        if rr >= 1.5:
            score += 1

        if rr >= 2:
            score += 1

        if risk > 0:
            score += 1

        if reward > 0:
            score += 1

        if earnings_blocker:
            score -= 2

        if status == "Swing Extended":
            score -= 3

        if not trend_up:
            score -= 2

        if rr < 1.5:
            score -= 1

        if risk <= 0:
            score -= 2

        if target_above_entry_pct > SWING_MAX_TARGET_PCT:
            score -= 2

        if entry_above_price_pct > SWING_MAX_ENTRY_ABOVE_PRICE_PCT:
            score -= 2

        if price_past_entry_pct > SWING_MAX_PRICE_PAST_ENTRY_PCT:
            score -= 2

        score = max(0, min(score, 10))

        if risk <= 0:
            blockers.append("invalid risk")

        if reward <= 0:
            blockers.append("no upside target")

        if rr < 1.5:
            blockers.append("risk/reward below 1.5")

        if status in ["Swing Pullback", "Swing Base"] and score < 6:
            blockers.append("swing score not strong enough yet")

        if invalid_setup_levels:
            blockers.append("invalid setup levels")

        if target_above_entry_pct > SWING_MAX_TARGET_PCT:
            blockers.append("target is too far above entry for normal swing mode")

        if entry_above_price_pct > SWING_MAX_ENTRY_ABOVE_PRICE_PCT:
            blockers.append("entry trigger is too far above current price")

        if entry_above_price_pct > SWING_BREAKOUT_CLOSE_PCT:
            blockers.append("price is not close enough to breakout trigger")

        if price_past_entry_pct > SWING_MAX_PRICE_PAST_ENTRY_PCT:
            blockers.append("price is too extended past the swing entry trigger")

        ready_now_allowed = (
            status in ["Swing Pullback", "Swing Base"]
            and trend_up
            and risk > 0
            and reward > 0
            and rr >= 1.5
            and target_above_entry_pct <= SWING_MAX_TARGET_PCT
            and entry_above_price_pct <= SWING_MAX_ENTRY_ABOVE_PRICE_PCT
            and price_past_entry_pct <= SWING_MAX_PRICE_PAST_ENTRY_PCT
            and not earnings_blocker
            and not invalid_setup_levels
        )

        setup_is_good_watch = (
            status in ["Swing Pullback", "Swing Base", "Swing Trend"]
            and trend_up
            and risk > 0
            and reward > 0
            and rr >= 1.5
            and target_above_entry_pct <= SWING_MAX_TARGET_PCT
            and not earnings_blocker
            and not invalid_setup_levels
        )

        if ready_now_allowed and price >= entry:
            decision = "TRADE"
            action = "READY NOW"
            entry_warning = "SWING READY ZONE"
            level_note = "Swing setup is actionable at the current price."

            if price_past_entry_pct > 0:
                entry_warning = "SWING TRIGGER RECLAIMED"
                level_note = "Swing trigger has been reclaimed and is not extended yet."
        elif setup_is_good_watch:
            decision = "WATCH"
            action = "WAIT FOR SWING BREAK"

            if entry_above_price_pct > SWING_MAX_ENTRY_ABOVE_PRICE_PCT:
                action = "WAIT"
        elif invalid_setup_levels:
            decision = "PASS"
            action = "PASS"

        sniper = "NO"

        if (
            decision == "TRADE"
            and action == "READY NOW"
            and status in ["Swing Pullback", "Swing Base"]
            and score >= 7
            and rr >= 1.5
            and risk > 0
            and reward > 0
            and trend_up
            and extended_from_sma20_pct <= 8
            and target_above_entry_pct <= SWING_MAX_TARGET_PCT
            and entry_above_price_pct <= SWING_MAX_ENTRY_ABOVE_PRICE_PCT
            and price_past_entry_pct <= SWING_MAX_PRICE_PAST_ENTRY_PCT
            and volume_ratio >= 0.70
            and not earnings_blocker
        ):
            sniper = "YES"

        grade = get_grade(score)

        confidence = get_swing_confidence(
            decision=decision,
            action=action,
            score=score,
            rr=rr,
            trend_up=trend_up,
            entry_above_price_pct=entry_above_price_pct,
            price_past_entry_pct=price_past_entry_pct,
            target_above_entry_pct=target_above_entry_pct,
            earnings_blocker=earnings_blocker,
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

        reason = build_swing_reason(
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
            price=price,
            entry=entry,
            stop=stop,
            target=target,
            level_note=level_note,
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
            "blockers": blockers,
            "reason": reason,
        }

        stock.update(build_hold_plan("swing", status, action, decision))
        stock = resolve_final_verdict(stock)

        return add_empty_position_fields(stock)

    except Exception as e:
        return empty_stock(symbol, str(e))


def get_stock_data(symbol, mode="quick"):
    mode = normalize_mode(mode)

    if mode == "swing":
        return get_swing_stock_data(symbol)

    return get_quick_stock_data(symbol)


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
        "WAIT FOR PULLBACK": 3,
        "WAIT FOR SWING BREAK": 4,
        "SWING WATCH": 5,
        "WATCH LOW PRIORITY": 6,
        "WAIT FOR VOLUME": 7,
        "WAIT FOR CONFIRMATION": 8,
        "WAIT FOR BETTER R/R": 9,
        "WAIT FOR CLEANER ENTRY": 10,
        "CHECK SETUP": 11,
        "WAIT": 12,
        "WAIT FOR RESET": 13,
        "TOO LATE / EXTENDED": 14,
        "NO TRADE": 15,
        "PASS": 16,
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
    sniper_bonus = 0 if (
        stock.get("decision") == "TRADE"
        and stock.get("sniper") == "YES"
    ) else 1

    return (
        active_bonus,
        focus_bonus,
        sniper_bonus,
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


def build_scanner_payload(mode):
    symbols = get_watchlist()
    active_positions = get_active_positions()

    stocks = [get_stock_data(s, mode) for s in symbols]
    stocks = apply_active_position_to_stocks(stocks, active_positions)

    stocks.sort(key=scanner_sort_key)
    stocks = add_focus_labels(stocks)
    active_trades = build_active_trades_summary(stocks, active_positions)

    return {
        "stocks": stocks,
        "active_positions": active_positions,
        "active_trades": active_trades,
        "mode": mode,
        "last_updated": datetime.now(ZoneInfo("America/New_York")).strftime("%I:%M:%S %p"),
    }


@app.route("/")
def home():
    mode = normalize_mode(request.args.get("mode", "quick"))
    payload = build_scanner_payload(mode)

    return render_template(
        "index.html",
        stocks=payload["stocks"],
        active_positions=payload["active_positions"],
        active_trades=payload["active_trades"],
        mode=payload["mode"],
        last_updated=payload["last_updated"],
    )


@app.route("/api/stocks")
def api_stocks():
    mode = normalize_mode(request.args.get("mode", "quick"))
    return jsonify(build_scanner_payload(mode))


@app.route("/position", methods=["POST"])
def set_position():
    mode = normalize_mode(request.form.get("mode", "quick"))

    symbol = request.form.get("position_symbol", "").upper().strip()
    entry_raw = request.form.get("position_entry", "").strip()
    shares_raw = request.form.get("position_shares", "").strip()
    capital_raw = request.form.get("position_capital", "").strip()
    trade_type = request.form.get("position_trade_type", "swing").strip()
    notes = request.form.get("position_notes", "").strip()

    try:
        entry = float(entry_raw)
        shares = float(shares_raw) if shares_raw else 0
        capital = float(capital_raw) if capital_raw else 0

        if symbol and entry > 0 and (shares > 0 or capital > 0):
            set_active_position(
                symbol,
                entry,
                shares=shares,
                capital=capital,
                trade_type=trade_type,
                notes=notes,
            )

    except ValueError:
        pass

    return redirect(url_for("home", mode=mode))


@app.route("/position/exit/<symbol>", methods=["POST"])
def exit_position(symbol):
    mode = normalize_mode(request.form.get("mode", "quick"))
    exit_price = request.form.get("exit_price", "").strip()
    exit_active_position(symbol, exit_price=exit_price)
    return redirect(url_for("home", mode=mode))


@app.route("/position/clear", methods=["POST"])
def clear_position():
    mode = normalize_mode(request.form.get("mode", "quick"))
    clear_active_position()
    return redirect(url_for("home", mode=mode))


@app.route("/add", methods=["POST"])
def add():
    mode = normalize_mode(request.form.get("mode", "quick"))
    add_symbol(request.form.get("symbol", ""))
    return redirect(url_for("home", mode=mode))


@app.route("/remove/<symbol>", methods=["POST"])
def remove(symbol):
    mode = normalize_mode(request.form.get("mode", "quick"))
    remove_symbol(symbol)
    return redirect(url_for("home", mode=mode))


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
