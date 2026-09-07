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
