"""
Interactive Telegram bot commands - polled once per scheduled run.

Telegram queues up any messages/button taps sent to the bot between
runs; getUpdates(offset=...) retrieves only what's new since the last
processed update_id (tracked in state.json under "telegram_offset").
Since we're on free GitHub Actions (not an always-on server), replies
land within the next run - up to ~15 minutes, which you already agreed
is fine.
"""

import json
import os

import requests

import config

API_BASE = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

MAIN_MENU = {
    "keyboard": [
        ["📊 My Outcome", "📰 Fundamentals"],
        ["🤖 Ask AI", "🆘 Support"],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


def get_updates(offset: int) -> list:
    try:
        resp = requests.get(
            f"{API_BASE}/getUpdates",
            params={"offset": offset, "timeout": 0},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            print(f"[bot] getUpdates not ok: {data}")
            return []
        return data.get("result", [])
    except Exception as exc:  # noqa: BLE001 - a bad poll should never crash the run
        print(f"[bot] getUpdates failed: {exc}")
        return []


def send_with_menu(text: str) -> None:
    try:
        requests.post(
            f"{API_BASE}/sendMessage",
            data={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": json.dumps(MAIN_MENU),
            },
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[bot] send_with_menu failed: {exc}")


def build_outcome_summary() -> str:
    if not os.path.exists(config.ALERTS_LOG_FILE):
        return "📊 No alerts logged yet - nothing to report."
    try:
        with open(config.ALERTS_LOG_FILE) as f:
            entries = json.load(f)
    except (json.JSONDecodeError, OSError):
        entries = []
    if not entries:
        return "📊 No alerts logged yet - nothing to report."

    by_strategy = {}
    for e in entries:
        by_strategy[e["strategy"]] = by_strategy.get(e["strategy"], 0) + 1

    lines = [f"📊 <b>Outcome so far</b> ({len(entries)} alerts total)"]
    for strat, count in sorted(by_strategy.items()):
        lines.append(f"- {strat}: {count}")
    lines.append("")
    lines.append("Note: this counts alerts SENT, not yet whether TP or SL "
                  "was hit - win/loss tracking is a planned next step.")
    return "\n".join(lines)


def support_text() -> str:
    return (
        "🆘 <b>Support</b>\n"
        "This bot is alert-only - it never places trades for you.\n"
        "For questions about a specific alert, check the strategy name "
        "against the rules defined for it.\n"
        "For anything about how the bot itself works, come back to this "
        "chat with Claude."
    )
