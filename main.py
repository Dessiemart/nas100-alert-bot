"""
Entry point. Run by GitHub Actions on a schedule.

Flow:
  0. Skip entirely if it's the weekend (markets closed) - saves API
     credits and Actions minutes.
  1. Work out which symbol/timeframe combos are actually worth fetching
     right now (see build_data_plan) - Twelve Data's free tier is
     credit-limited, so we only pull extra timeframes during the
     killzone they're relevant to, plus cheap 1H trend context always.
  2. Fetch that data from Twelve Data.
  3. Run all five strategies against it.
  4. For each new confirmed setup: skip it if high-impact news is
     imminent; otherwise attach a suggested position size, send it to
     Telegram, and log it for later review.
  5. Run the partial-confluence progress checkers and send "X/Y
     confluences" heads-ups for setups one step from confirming.
  6. Once a day, send a heartbeat summary so you know the bot is alive
     even on quiet days.

State (state.json) and the outcome log (alerts_log.json) are committed
back to the repo by the workflow after each run.
"""

import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from src.twelvedata_client import fetch_market_data
from src.strategies import ALL_STRATEGIES
from src.confluence import ALL_PROGRESS_CHECKERS
from src.telegram_alert import send_telegram_message, format_alert
from src.killzones import (
    NY_TZ, ASIAN, LONDON, NEW_YORK_AM, is_weekend_market_closed,
)
from src.candles import utc_now
from src.news_calendar import upcoming_high_impact_event
from src.position_sizing import suggested_lot_size, format_size_note
from src import telegram_bot


def build_data_plan(now) -> list[tuple[str, str]]:
    """Trim which symbol/timeframe combos we fetch based on which
    killzone is currently relevant, to stay within Twelve Data's free
    credit budget (see config.TWELVEDATA_DAILY_CREDIT_BUDGET)."""
    plan = [("NAS100", "1H"), ("XAUUSD", "1H"), ("EURUSD", "1H")]  # always - cheap trend context

    in_asian_or_grace = ASIAN.contains(now) or ASIAN.contains(now - timedelta(hours=3))
    in_london_or_grace = LONDON.contains(now) or LONDON.contains(now - timedelta(hours=1))
    in_ny_am = NEW_YORK_AM.contains(now)

    if in_asian_or_grace:
        plan += [("NAS100", "5M"), ("NAS100", "15M")]
    if in_london_or_grace:
        plan += [("NAS100", "5M"), ("NAS100", "15M"), ("NAS100", "30M"),
                 ("XAUUSD", "5M"), ("XAUUSD", "15M"), ("XAUUSD", "30M")]
    if in_ny_am:
        plan += [("NAS100", "1M"), ("XAUUSD", "1M"), ("EURUSD", "1M")]

    # de-duplicate while preserving order
    seen = set()
    deduped = []
    for item in plan:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def load_state() -> dict:
    if os.path.exists(config.STATE_FILE):
        with open(config.STATE_FILE) as f:
            state = json.load(f)
    else:
        state = {}
    state.setdefault("seen_keys", [])
    state.setdefault("last_heartbeat_ny_date", None)
    state.setdefault("alerts_today", 0)
    state.setdefault("near_miss_today", 0)
    state.setdefault("telegram_offset", 0)
    state.setdefault("menu_sent", False)
    return state


def save_state(state: dict) -> None:
    state["seen_keys"] = state["seen_keys"][-500:]
    with open(config.STATE_FILE, "w") as f:
        json.dump(state, f)


def append_alert_log(alert) -> None:
    entries = []
    if os.path.exists(config.ALERTS_LOG_FILE):
        try:
            with open(config.ALERTS_LOG_FILE) as f:
                entries = json.load(f)
        except (json.JSONDecodeError, OSError):
            entries = []

    entries.append({
        "sent_at_utc": datetime.utcnow().isoformat(),
        "strategy": alert.strategy,
        "symbol": alert.symbol,
        "direction": alert.direction,
        "entry_type": alert.entry_type,
        "entry": alert.entry,
        "sl": alert.sl,
        "tp": alert.tp,
    })
    entries = entries[-config.ALERTS_LOG_MAX_ENTRIES:]

    with open(config.ALERTS_LOG_FILE, "w") as f:
        json.dump(entries, f)


def maybe_send_heartbeat(state: dict) -> None:
    today_ny = datetime.now(NY_TZ).date().isoformat()
    if state["last_heartbeat_ny_date"] == today_ny:
        return  # already sent today

    if state["last_heartbeat_ny_date"] is not None:
        text = (
            "✅ <b>Daily check-in</b> — bot ran fine.\n"
            f"Alerts sent: {state['alerts_today']}\n"
            f"Near-miss heads-ups: {state['near_miss_today']}"
        )
        send_telegram_message(text)

    state["last_heartbeat_ny_date"] = today_ny
    state["alerts_today"] = 0
    state["near_miss_today"] = 0


def check_bot_commands(state: dict) -> None:
    if not state["menu_sent"]:
        telegram_bot.send_with_menu(
            "👋 <b>Bot online.</b> Pick an option below any time - "
            "replies may take a few minutes since I check every ~15 min."
        )
        state["menu_sent"] = True

    updates = telegram_bot.get_updates(state["telegram_offset"] + 1)
    for update in updates:
        state["telegram_offset"] = update["update_id"]
        text = update.get("message", {}).get("text", "").strip()

        if text in ("/start", "/menu"):
            telegram_bot.send_with_menu("👋 Here's the menu.")
        elif text == "📊 My Outcome":
            telegram_bot.send_with_menu(telegram_bot.build_outcome_summary())
        elif text == "🆘 Support":
            telegram_bot.send_with_menu(telegram_bot.support_text())
        elif text == "📰 Fundamentals":
            telegram_bot.send_with_menu(
                "📰 Fundamentals by symbol isn't built yet - coming in the next phase."
            )
        elif text == "🤖 Ask AI":
            telegram_bot.send_with_menu(
                "🤖 AI analysis isn't wired up yet - coming once the AI API key is set up."
            )


def main() -> None:
    now = utc_now()
    state = load_state()
    check_bot_commands(state)

    if is_weekend_market_closed(now):
        print("[main] weekend - markets closed, skipping this run.")
        save_state(state)
        return

    data_plan = build_data_plan(now)
    print(f"[main] fetching {len(data_plan)} symbol/timeframe combos from Twelve Data: {data_plan}")
    market_data = fetch_market_data(data_plan)
    data = market_data["trendbars"]
    balance = market_data["balance"]
    symbol_specs = market_data["symbols"]
    for (symbol, period), candles in data.items():
        print(f"[main] {symbol} {period}: {len(candles)} candles")

    seen = set(state["seen_keys"])
    new_alerts = []

    for strategy_fn in ALL_STRATEGIES:
        try:
            alerts = strategy_fn(data)
        except Exception as exc:  # noqa: BLE001 - one bad strategy shouldn't kill the run
            print(f"[main] strategy {strategy_fn.__name__} raised: {exc}")
            continue
        for alert in alerts:
            if alert.key in seen:
                continue
            new_alerts.append(alert)

    news_event = upcoming_high_impact_event(now, config.NEWS_PAUSE_BUFFER_MINUTES)
    if news_event:
        print(f"[main] high-impact news nearby ({news_event.get('title', 'unknown event')}) "
              f"- holding back full alerts this run, will retry next run.")

    for alert in new_alerts:
        if news_event:
            continue  # don't mark as seen - retry once the news window passes

        seen.add(alert.key)
        spec = symbol_specs.get(alert.symbol)
        lots = suggested_lot_size(balance, config.RISK_PERCENT, alert.entry, alert.sl, spec) if spec else None
        size_note = format_size_note(balance, lots, config.RISK_PERCENT)
        full_note = f"{alert.note}\n{size_note}" if alert.note else size_note

        text = format_alert(
            strategy=alert.strategy, symbol=alert.symbol, direction=alert.direction,
            entry_type=alert.entry_type, entry=alert.entry, sl=alert.sl, tp=alert.tp,
            note=full_note,
        )
        print(f"[main] sending alert: {alert.key}")
        send_telegram_message(text)
        append_alert_log(alert)
        state["alerts_today"] += 1

    if not new_alerts:
        print("[main] no new setups this run.")

    new_progress = []
    for checker_fn in ALL_PROGRESS_CHECKERS:
        try:
            checks = checker_fn(data)
        except Exception as exc:  # noqa: BLE001
            print(f"[main] progress checker {checker_fn.__name__} raised: {exc}")
            continue
        for check in checks:
            if not check.is_near_complete():
                continue
            if check.key in seen:
                continue
            new_progress.append(check)
            seen.add(check.key)

    for check in new_progress:
        print(f"[main] sending near-miss: {check.key}")
        send_telegram_message(check.format_message())
        state["near_miss_today"] += 1

    if not new_progress:
        print("[main] no near-miss setups this run.")

    maybe_send_heartbeat(state)

    state["seen_keys"] = list(seen)
    save_state(state)


if __name__ == "__main__":
    main()
