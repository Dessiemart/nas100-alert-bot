"""
Optional high-impact news pause. Uses a free, no-key-required weekly
economic calendar feed (ForexFactory's public JSON feed, widely used by
EA developers). If it can't be reached or its format has changed, alerts
still send as normal - a missing/broken calendar should never silently
block a real signal.

FLAGGING FOR VERIFICATION: this feed's exact field names aren't
guaranteed stable and haven't been tested against live data in this
build. If news-pause behavior looks wrong once running for real (never
pausing, or erroring), check the printed [news] log lines in the Actions
run log first - the parsing may need a small adjustment to match the
feed's current field names.
"""

from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

import config

from src.killzones import NY_TZ

_CACHE = {"fetched_at": None, "events": []}


def _fetch_calendar() -> list:
    try:
        resp = requests.get(config.NEWS_CALENDAR_URL, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001 - a broken calendar should never crash the run
        print(f"[news] could not fetch/parse calendar, skipping news pause this run: {exc}")
        return []


def _get_events() -> list:
    now = datetime.now(timezone.utc)
    if _CACHE["fetched_at"] is None or (now - _CACHE["fetched_at"]) > timedelta(hours=6):
        _CACHE["events"] = _fetch_calendar()
        _CACHE["fetched_at"] = now
    return _CACHE["events"]


def upcoming_high_impact_event(now_utc: datetime, buffer_minutes: int) -> Optional[dict]:
    """Return the event dict if a high-impact event for a watched
    currency falls within +/- buffer_minutes of now, else None."""
    if not config.NEWS_PAUSE_ENABLED:
        return None

    events = _get_events()
    window = timedelta(minutes=buffer_minutes)
    for event in events:
        try:
            if event.get("impact") != "High":
                continue
            if event.get("country") not in config.WATCHED_NEWS_CURRENCIES:
                continue
            raw_date = event.get("date", "")
            event_time = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if abs((event_time - now_utc).total_seconds()) <= window.total_seconds():
            return event
    return None


# ---------------------------------------------------------------------------
# Fundamentals feature - daily digest + on-demand by symbol
# ---------------------------------------------------------------------------

def _parse_event_time(event: dict):
    try:
        return datetime.fromisoformat(event["date"])
    except Exception:
        return None


def get_events_in_range(start_utc: datetime, end_utc: datetime, currencies: list) -> list:
    """Events for the given currencies whose time falls in [start_utc, end_utc),
    sorted chronologically. Excludes Holiday entries (not useful for
    fundamentals). Confirmed against the real feed - no 'actual' field
    exists even for near-term events, so we only ever show forecast/previous."""
    events = _get_events()
    out = []
    for event in events:
        if event.get("country") not in currencies:
            continue
        if event.get("impact") == "Holiday":
            continue
        event_time = _parse_event_time(event)
        if event_time is None:
            continue
        if start_utc <= event_time < end_utc:
            out.append((event_time, event))
    out.sort(key=lambda pair: pair[0])
    return out


def _impact_emoji(impact: str) -> str:
    return {"High": "🔴", "Medium": "🟠", "Low": "⚪"}.get(impact, "⚪")


def _format_event_line(event_time: datetime, event: dict) -> str:
    ny_time = event_time.astimezone(NY_TZ) if event_time.tzinfo else event_time
    time_str = ny_time.strftime("%a %H:%M NY")
    forecast = event.get("forecast") or "-"
    previous = event.get("previous") or "-"
    return (f"{_impact_emoji(event.get('impact'))} {time_str} — {event.get('title')} "
            f"({event.get('country')})\n    Forecast: {forecast} | Previous: {previous}")


def build_symbol_fundamentals(symbol: str, now_utc: datetime) -> str:
    currencies = config.FUNDAMENTALS_CURRENCIES_BY_SYMBOL.get(symbol, [])
    if not currencies:
        return f"No fundamentals mapping configured for {symbol}."

    window_end = now_utc + timedelta(hours=48)
    events = get_events_in_range(now_utc, window_end, currencies)
    events = [(t, e) for t, e in events if e.get("impact") in ("High", "Medium")]

    lines = [f"📰 <b>{symbol} fundamentals</b> — next 48h, {'/'.join(currencies)}"]
    if not events:
        lines.append("No high/medium-impact events scheduled in this window.")
    else:
        for event_time, event in events[:8]:
            lines.append(_format_event_line(event_time, event))
    lines.append("")
    lines.append("Forecast/previous only - actual results aren't available "
                  "from this feed until well after release, if at all.")
    return "\n".join(lines)


def build_daily_digest(now_utc: datetime) -> str:
    window_end = now_utc + timedelta(hours=24)
    events = get_events_in_range(now_utc, window_end, config.DAILY_DIGEST_CURRENCIES)
    events = [(t, e) for t, e in events if e.get("impact") in ("High", "Medium")]

    lines = ["📰 <b>Today's fundamentals</b> (next 24h, USD/EUR)"]
    if not events:
        lines.append("No high/medium-impact events in the next 24h.")
    else:
        for event_time, event in events[:10]:
            lines.append(_format_event_line(event_time, event))
    return "\n".join(lines)
