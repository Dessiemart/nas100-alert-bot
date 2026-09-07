"""
Entry point. Run by GitHub Actions on a schedule.

Flow:
  1. Work out which killzones are relevant right now (we fetch data
     generously so strategies can also look at "the most recently
     completed" session, not just a currently-active one).
  2. Pull all the candle data every strategy needs, in one short-lived
     cTrader connection.
  3. Run all five strategies against that data.
  4. De-duplicate against state.json (committed back to the repo) so the
     same setup doesn't re-alert every 5 minutes until it's gone stale.
  5. Send any new alerts to Telegram.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from src.ctrader_client import fetch_all_trendbars
from src.strategies import ALL_STRATEGIES
from src.confluence import ALL_PROGRESS_CHECKERS
from src.telegram_alert import send_telegram_message, format_alert

# Every (symbol, period) pair any strategy might need. Strategies just
# read from this shared pool - fetching a superset once is far cheaper
# than each strategy opening its own connection.
DATA_PLAN = [
    ("NAS100", "1M"), ("NAS100", "5M"), ("NAS100", "15M"), ("NAS100", "30M"), ("NAS100", "1H"),
    ("XAUUSD", "1M"), ("XAUUSD", "5M"), ("XAUUSD", "15M"), ("XAUUSD", "30M"), ("XAUUSD", "1H"),
    ("EURUSD", "1M"), ("EURUSD", "1H"),
]


def load_state() -> dict:
    if os.path.exists(config.STATE_FILE):
        with open(config.STATE_FILE) as f:
            return json.load(f)
    return {"seen_keys": []}


def save_state(state: dict) -> None:
    # Keep the seen-keys list from growing forever.
    state["seen_keys"] = state["seen_keys"][-500:]
    with open(config.STATE_FILE, "w") as f:
        json.dump(state, f)


def main() -> None:
    print("[main] fetching candle data from cTrader...")
    data = fetch_all_trendbars(DATA_PLAN)
    for (symbol, period), candles in data.items():
        print(f"[main] {symbol} {period}: {len(candles)} candles")

    state = load_state()
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
            seen.add(alert.key)

    for alert in new_alerts:
        text = format_alert(
            strategy=alert.strategy, symbol=alert.symbol, direction=alert.direction,
            entry_type=alert.entry_type, entry=alert.entry, sl=alert.sl, tp=alert.tp,
            note=alert.note,
        )
        print(f"[main] sending alert: {alert.key}")
        send_telegram_message(text)

    if not new_alerts:
        print("[main] no new setups this run.")

    # Partial-confluence heads-up: setups that are one step away from a
    # full signal, even though they haven't fully confirmed yet.
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

    if not new_progress:
        print("[main] no near-miss setups this run.")

    state["seen_keys"] = list(seen)
    save_state(state)


if __name__ == "__main__":
    main()
