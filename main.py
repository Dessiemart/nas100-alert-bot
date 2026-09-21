"""
Entry point. Run by GitHub Actions on a schedule.

All 3 symbols (NAS100, XAUUSD, EURUSD) come from cTrader in a single
connection per run. NAS100's 15M/1H/4H/1D are always fetched (not just
during killzones) and written to nas100_snapshot.json, committed to
this repo - that's what lets the Apps Script AI-chat feature analyze
NAS100 too, since cTrader needs a persistent connection Apps Script
can't hold open itself; this bot already holds that connection every
5 minutes and can just save the result.

Flow:
  0. Skip entirely if it's the weekend (markets closed). Can be
     overridden for manual testing via the FORCE_RUN_WEEKEND env var.
  1. Work out which symbol/timeframe combos are worth fetching right
     now (see build_data_plan).
  2. Fetch all of it from cTrader in one connection.
  3. Write the NAS100 snapshot file for the AI-chat feature.
  4. Run all five strategies against it.
  5. For each new confirmed setup: skip it if high-impact news is
     imminent; otherwise attach a suggested position size, send it to
     Telegram, and log it for later review.
  6. Run the partial-confluence progress checkers and send "X/Y
     confluences" heads-ups for setups with at least one step confirmed.
  7. Once a day, send a heartbeat summary so you know the bot is alive
     even on quiet days.

State (state.json), the outcome log (alerts_log.json), and the NAS100
snapshot (nas100_snapshot.json) are committed back to the repo by the
workflow after each run.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from src.ctrader_client import fetch_trendbars
from src.strategies import ALL_STRATEGIES
from src.confluence import ALL_PROGRESS_CHECKERS
from src.telegram_alert import send_telegram_message, format_alert
from src.killzones import (
    NY_TZ, ASIAN, LONDON, NEW_YORK_AM, is_weekend_market_closed,
)
from src.candles import utc_now
from src.news_calendar import upcoming_high_impact_event, build_daily_digest
from src.position_sizing import suggested_lot_size, format_size_note, SymbolSpec
from src import telegram_bot


def build_data_plan(now) -> list:
    """Trim which symbol/timeframe combos we fetch based on which
    killzone is currently relevant - and skip any symbol disabled from
    the dashboard (config.ALL_SYMBOLS, sourced from settings.json).

    NAS100's 15M/4H/1D are always included (not just during killzones)
    so the AI-chat snapshot file below is always fully populated,
    regardless of what session is currently active - cTrader has no
    per-call credit cost, so this is essentially free."""
    plan = [(s, "1H") for s in ("NAS100", "XAUUSD", "EURUSD")]  # always - cheap trend context
    plan += [("NAS100", "15M"), ("NAS100", "4H"), ("NAS100", "1D")]
    plan += [(s, "15M") for s in ("XAUUSD", "EURUSD")]  # needed for session-agnostic strategies 6/7/10
    plan += [(s, "15M") for s in ("US500", "XAGUSD", "GBPUSD")]  # correlated pairs for SMT strategies 8/9

    in_asian_or_grace = ASIAN.contains(now) or ASIAN.contains(now - timedelta(hours=3))
    in_london_or_grace = LONDON.contains(now) or LONDON.contains(now - timedelta(hours=1))
    in_ny_am = NEW_YORK_AM.contains(now)

    if in_asian_or_grace:
        plan += [("NAS100", "5M"), ("NAS100", "15M"),
                 ("XAUUSD", "5M"), ("XAUUSD", "15M"),
                 ("EURUSD", "5M"), ("EURUSD", "15M")]
    if in_london_or_grace:
        plan += [("NAS100", "5M"), ("NAS100", "15M"), ("NAS100", "30M"),
                 ("XAUUSD", "5M"), ("XAUUSD", "15M"), ("XAUUSD", "30M"),
                 ("EURUSD", "5M"), ("EURUSD", "15M"), ("EURUSD", "30M")]
    if in_ny_am:
        plan += [("NAS100", "1M"), ("XAUUSD", "1M"), ("EURUSD", "1M")]

    correlated_symbols = ("US500", "XAGUSD", "GBPUSD")
    plan = [(symbol, period) for symbol, period in plan
            if symbol in config.ALL_SYMBOLS or symbol in correlated_symbols]

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
    state.setdefault("last_fundamentals_digest_date", None)
    return state


def save_state(state: dict) -> None:
    state["seen_keys"] = state["seen_keys"][-500:]
    with open(config.STATE_FILE, "w") as f:
        json.dump(state, f)


def write_nas100_snapshot(data: dict) -> None:
    """Writes NAS100's 15M/1H/4H/1D candles to a JSON file committed to
    this repo, so the Apps Script AI-chat feature can read real NAS100
    structure via GitHub's Contents API - cTrader needs a persistent
    connection Apps Script can't hold open itself, so this file is the
    workaround: this bot already holds that connection every 5 minutes."""
    snapshot = {}
    for period_label in ("15M", "1H", "4H", "1D"):
        candles = data.get(("NAS100", period_label), [])
        snapshot[period_label] = [
            {"time": c.time.isoformat(), "open": c.open, "high": c.high, "low": c.low, "close": c.close}
            for c in candles
        ]
    with open("nas100_snapshot.json", "w") as f:
        json.dump(snapshot, f)
def write_live_setups(all_checks: list) -> None:
    """Writes the currently-building confluence setups to a file the
    dashboard reads directly - matches the exact shape pages/index.js
    expects: {updated_at_utc, setups: [{key, strategy, symbol, leaning,
    confirmed, total, steps: [{label, ok}]}]}. Only includes setups
    with at least 1 confirmed step, sorted most-complete first."""
    setups = []
    for check in all_checks:
        if check.confirmed == 0:
            continue
        leaning = "BUY" if check.direction == "buy" else ("SELL" if check.direction == "sell" else None)
        setups.append({
            "key": check.key,
            "strategy": check.strategy,
            "symbol": check.symbol,
            "leaning": leaning,
            "confirmed": check.confirmed,
            "total": check.total,
            "steps": [
                {"label": name, "ok": ok}
                for name, ok in zip(check.step_names, check.step_results)
            ],
        })
    setups.sort(key=lambda s: s["confirmed"] / max(s["total"], 1), reverse=True)
    payload = {
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "setups": setups,
    }
    with open("live_setups.json", "w") as f:
        json.dump(payload, f)

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
        return

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


def maybe_send_daily_fundamentals(state: dict, now) -> None:
    today_ny = now.astimezone(NY_TZ).date().isoformat()
    if state.get("last_fundamentals_digest_date") == today_ny:
        return
    telegram_bot.send_with_menu(build_daily_digest(now))
    state["last_fundamentals_digest_date"] = today_ny


def build_symbol_specs() -> dict:
    """All 3 symbols come from cTrader - these are the same assumed
    contract sizes config.py already flags for verification."""
    return {
        symbol: SymbolSpec(
            lot_size=config.ASSUMED_LOT_SIZE[symbol], min_volume=1, max_volume=10000000, step_volume=1,
        )
        for symbol in config.ALL_SYMBOLS
    }


def main() -> None:
    now = utc_now()
    state = load_state()

    force_run = os.environ.get("FORCE_RUN_WEEKEND") == "true"
    if is_weekend_market_closed(now) and not force_run:
        print("[main] weekend - markets closed, skipping this run.")
        save_state(state)
        return
    if is_weekend_market_closed(now) and force_run:
        print("[main] weekend override active - proceeding anyway for testing.")

    data_plan = build_data_plan(now)
    print(f"[main] fetching from cTrader: {data_plan}")

    data = fetch_trendbars(data_plan)
    write_nas100_snapshot(data)
    symbol_specs = build_symbol_specs()

    balance = config.ACCOUNT_BALANCE
    for (symbol, period), candles in data.items():
        print(f"[main] {symbol} {period}: {len(candles)} candles")

    seen = set(state["seen_keys"])
    new_alerts = []

    for strategy_fn in ALL_STRATEGIES:
        try:
            alerts = strategy_fn(data)
        except Exception as exc:  # noqa: BLE001
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
            continue

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

     all_current_checks = []
    new_progress = []
    for checker_fn in ALL_PROGRESS_CHECKERS:
        try:
            checks = checker_fn(data)
        except Exception as exc:  # noqa: BLE001
            print(f"[main] progress checker {checker_fn.__name__} raised: {exc}")
            continue
        all_current_checks.extend(checks)
        for check in checks:
            if check.confirmed == 0:
                continue
            if check.key in seen:
                continue
            new_progress.append(check)
            seen.add(check.key)

    write_live_setups(all_current_checks)

    for check in new_progress:
        print(f"[main] sending near-miss: {check.key}")
        send_telegram_message(check.format_message())
        state["near_miss_today"] += 1

    if not new_progress:
        print("[main] no near-miss setups this run.")

    maybe_send_heartbeat(state)
    maybe_send_daily_fundamentals(state, now)

    state["seen_keys"] = list(seen)
    save_state(state)


if __name__ == "__main__":
    main()
