"""Sends alert messages to your Telegram chat via the Bot API."""

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


def send_telegram_message(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    ok = resp.ok and resp.json().get("ok", False)
    if not ok:
        print(f"[telegram] send failed: {resp.status_code} {resp.text}")
    return ok


def format_alert(strategy: str, symbol: str, direction: str, entry_type: str,
                  entry: float, sl: float, tp: float, note: str = "") -> str:
    arrow = "🟢 BUY" if direction.lower().startswith("b") else "🔴 SELL"
    lines = [
        f"<b>{strategy}</b> — {symbol}",
        f"{arrow} ({entry_type})",
        f"Entry: {entry:.5g}",
        f"SL: {sl:.5g}",
        f"TP: {tp:.5g}",
    ]
    if note:
        lines.append(note)
    lines.append("⚠️ Alert only — no order was placed. Confirm manually before trading.")
    return "\n".join(lines)
