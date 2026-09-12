"""
One-off, read-only diagnostic. Checks every independent piece of the
bot separately and reports ✅/🔴 for each to the Job Summary tab, so
you get the whole picture in one run instead of chasing one error at
a time.

Nothing here writes any secrets or sends any Telegram messages except
where explicitly noted. Safe to run any time. NOTE: this DOES perform
a real cTrader token refresh (not a dry run) - running it rotates your
refresh token for real, same as any normal bot run.
"""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests


def summary(line: str):
    print(line)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(line + "\n")


def check_secrets_present():
    summary("## 1. Secrets wiring")
    names = [
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TWELVEDATA_API_KEY",
        "CTRADER_CLIENT_ID", "CTRADER_CLIENT_SECRET", "CTRADER_REFRESH_TOKEN",
        "CTRADER_ACCOUNT_ID", "SYMBOL_ID_NAS100", "GH_PAT_FOR_SECRETS",
    ]
    for name in names:
        value = os.environ.get(name)
        if value:
            summary(f"✅ {name} — present (length {len(value)})")
        else:
            summary(f"🔴 {name} — MISSING or empty")


def check_weekend_guard():
    summary("\n## 2. Weekend guard")
    try:
        from src.killzones import is_weekend_market_closed, NY_TZ
        now = datetime.now(timezone.utc)
        ny_now = now.astimezone(NY_TZ)
        closed = is_weekend_market_closed(now)
        summary(f"Current UTC time: {now.isoformat()}")
        summary(f"Current NY time: {ny_now.isoformat()} ({ny_now.strftime('%A')})")
        summary(f"is_weekend_market_closed() returns: **{closed}**")
        force_run = os.environ.get("FORCE_RUN_WEEKEND")
        summary(f"FORCE_RUN_WEEKEND env var received as: `{force_run!r}`")
        if closed and force_run != "true":
            summary("ℹ️ Markets are closed AND override is not active — a normal scheduled "
                     "or manual run right now would skip everything after this point.")
    except Exception as exc:  # noqa: BLE001
        summary(f"🔴 Weekend guard check crashed: {exc}")


def check_ctrader_refresh():
    summary("\n## 3. cTrader token refresh")
    try:
        import config
        resp = requests.get(
            "https://openapi.ctrader.com/apps/token",
            params={
                "grant_type": "refresh_token",
                "refresh_token": config.CTRADER_REFRESH_TOKEN,
                "client_id": config.CTRADER_CLIENT_ID,
                "client_secret": config.CTRADER_CLIENT_SECRET,
            },
            headers={"Accept": "application/json"},
            timeout=20,
        )
        data = resp.json()
        if data.get("errorCode"):
            summary(f"🔴 cTrader rejected the refresh: {data.get('errorCode')} — {data.get('description', '')}")
            summary("This means CTRADER_REFRESH_TOKEN is stale. You'll need the browser "
                     "OAuth flow again (Steps 1-2 in README.md) to get a fresh one.")
        elif data.get("accessToken"):
            summary(f"✅ Refresh succeeded — got a new access token (length {len(data['accessToken'])}).")
            summary("⚠️ Note: running this diagnostic just rotated your refresh token. "
                     "If GH_PAT_FOR_SECRETS isn't working, update CTRADER_ACCESS_TOKEN and "
                     "CTRADER_REFRESH_TOKEN secrets manually with the values below.")
            summary(f"New CTRADER_ACCESS_TOKEN: `{data['accessToken']}`")
            if data.get("refreshToken"):
                summary(f"New CTRADER_REFRESH_TOKEN: `{data['refreshToken']}`")
        else:
            summary(f"🔴 Unexpected response shape: {data}")
    except Exception as exc:  # noqa: BLE001
        summary(f"🔴 cTrader refresh check crashed: {exc}")


def check_gh_pat_for_secrets():
    summary("\n## 4. GH_PAT_FOR_SECRETS permission check (read-only, writes nothing)")
    pat = os.environ.get("GH_PAT_FOR_SECRETS")
    if not pat:
        summary("🔴 GH_PAT_FOR_SECRETS is not set — the self-healing token fix cannot work at all.")
        return
    try:
        resp = requests.get(
            "https://api.github.com/repos/Dessiemart/nas100-alert-bot/actions/secrets/public-key",
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "application/vnd.github+json",
            },
            timeout=15,
        )
        if resp.status_code == 200:
            summary("✅ PAT can read the repo's secrets public key — permissions look correct.")
        elif resp.status_code in (401, 403):
            summary(f"🔴 PAT rejected (HTTP {resp.status_code}) — check it's scoped to this repo "
                     f"with 'Secrets: Read and write' permission, and hasn't expired.")
        else:
            summary(f"🔴 Unexpected HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as exc:  # noqa: BLE001
        summary(f"🔴 GH_PAT_FOR_SECRETS check crashed: {exc}")


def check_twelvedata():
    summary("\n## 5. Twelve Data reachability")
    try:
        import config
        resp = requests.get(
            "https://api.twelvedata.com/time_series",
            params={"symbol": "EUR/USD", "interval": "1h", "outputsize": 1, "apikey": config.TWELVEDATA_API_KEY},
            timeout=20,
        )
        data = resp.json()
        if data.get("status") == "error" or data.get("code"):
            summary(f"🔴 Twelve Data returned an error: {data}")
        elif "values" in data:
            summary(f"✅ Twelve Data reachable — got {len(data['values'])} candle(s) for EUR/USD.")
        else:
            summary(f"🔴 Unexpected response shape: {data}")
    except Exception as exc:  # noqa: BLE001
        summary(f"🔴 Twelve Data check crashed: {exc}")


def check_news_calendar():
    summary("\n## 6. News calendar reachability")
    try:
        import config
        resp = requests.get(config.NEWS_CALENDAR_URL, timeout=20)
        data = resp.json()
        if isinstance(data, list):
            summary(f"✅ News calendar reachable — {len(data)} events this week.")
        else:
            summary(f"🔴 Unexpected shape (expected a list): {type(data)}")
    except Exception as exc:  # noqa: BLE001
        summary(f"🔴 News calendar check crashed (this is non-fatal in main.py — it just "
                 f"skips the news pause feature): {exc}")


def check_settings_json():
    summary("\n## 7. Dashboard settings.json")
    if os.path.exists("settings.json"):
        try:
            with open("settings.json") as f:
                data = json.load(f)
            summary(f"ℹ️ settings.json exists and overrides defaults: {json.dumps(data)}")
            import config
            summary(f"Resulting config.ALL_SYMBOLS after override: {config.ALL_SYMBOLS}")
        except Exception as exc:  # noqa: BLE001
            summary(f"🔴 settings.json exists but failed to parse: {exc}")
    else:
        summary("✅ No settings.json — using all default settings, all 3 symbols enabled.")


if __name__ == "__main__":
    summary("# Full Diagnostic Report")
    summary(f"Run at: {datetime.now(timezone.utc).isoformat()} UTC\n")
    check_secrets_present()
    check_weekend_guard()
    check_settings_json()
    check_ctrader_refresh()
    check_gh_pat_for_secrets()
    check_twelvedata()
    check_news_calendar()
    summary("\n---\nEnd of diagnostic. Nothing above sends alerts or touches state.json.")
