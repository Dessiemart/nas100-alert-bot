"""
Central configuration for the alert engine.

Everything sensitive comes from environment variables, which GitHub
Actions injects from your repository Secrets. Nothing here is a real
credential - it's all `os.environ[...]` lookups.

Required GitHub Secrets (Settings -> Secrets and variables -> Actions):
  TELEGRAM_BOT_TOKEN       - from BotFather
  TELEGRAM_CHAT_ID         - your Telegram chat id
  TWELVEDATA_API_KEY       - free key from twelvedata.com/register (Gold + EURUSD)
  CTRADER_CLIENT_ID        - MarketBot app Client ID (NAS100 only - Twelve
  CTRADER_CLIENT_SECRET      Data's free plan doesn't include US indices)
  CTRADER_REFRESH_TOKEN
  CTRADER_ACCOUNT_ID
  SYMBOL_ID_NAS100         - numeric symbolId for "US Tech 100 Index"
"""

import os


def _env(name: str, required: bool = True, default: str = None) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(
            f"Missing required environment variable/secret: {name}. "
            f"Add it under repo Settings -> Secrets and variables -> Actions."
        )
    return value


# ---- Telegram ----
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID")

# ---- Twelve Data - no longer used for market data (all 3 symbols now
# come from cTrader), kept optional in case it's ever needed again ----
TWELVEDATA_API_KEY = _env("TWELVEDATA_API_KEY", required=False, default="")
TWELVEDATA_SYMBOLS = {
    "XAUUSD": "XAU/USD",
    "EURUSD": "EUR/USD",
}

# ---- cTrader (all 3 symbols) ----
CTRADER_CLIENT_ID = _env("CTRADER_CLIENT_ID")
CTRADER_CLIENT_SECRET = _env("CTRADER_CLIENT_SECRET")
CTRADER_REFRESH_TOKEN = _env("CTRADER_REFRESH_TOKEN")
CTRADER_ACCOUNT_ID = int(_env("CTRADER_ACCOUNT_ID"))
CTRADER_SYMBOL_ID_NAS100 = int(_env("SYMBOL_ID_NAS100"))
CTRADER_SYMBOL_ID_XAUUSD = int(_env("SYMBOL_ID_XAUUSD"))
CTRADER_SYMBOL_ID_EURUSD = int(_env("SYMBOL_ID_EURUSD"))
CTRADER_SYMBOL_ID_US500 = int(_env("SYMBOL_ID_US500"))
CTRADER_SYMBOL_ID_XAGUSD = int(_env("SYMBOL_ID_XAGUSD"))
CTRADER_SYMBOL_ID_GBPUSD = int(_env("SYMBOL_ID_GBPUSD"))
CTRADER_IS_LIVE = _env("CTRADER_IS_LIVE", required=False, default="false").lower() == "true"

# ---- Account balance for position sizing ----
# Neither Twelve Data nor this scoped-down cTrader usage fetches a live
# balance - this is a number you maintain yourself.
ACCOUNT_BALANCE = 1000.0

# ---- Assumed contract sizes (units per 1.0 lot) for position sizing ----
# FLAG FOR VERIFICATION: EURUSD 100,000 units/lot and XAUUSD 100 oz/lot
# are essentially universal; NAS100 CFD contract size varies by broker -
# check Pepperstone's actual spec and adjust if it's not 1.0.
ASSUMED_LOT_SIZE = {
    "NAS100": 1.0,
    "XAUUSD": 100.0,
    "EURUSD": 100000.0,
}
RISK_PERCENT = 0.05

# All symbols the bot trades, regardless of data source - used by
# strategies, confluence, position sizing, and the bot menu. Overridden
# further down once settings.json is read.
ALL_SYMBOLS = ["NAS100", "XAUUSD", "EURUSD"]

# ---- SL buffers per strategy 4/5 ("newyork" / "newyork tt") ----
NAS100_SL_BUFFER_POINTS = 5.0
XAUUSD_SL_BUFFER = 0.50
EURUSD_SL_BUFFER_PIPS = 3.0
EURUSD_PIP = 0.0001

# ---- Swing (fractal) widths per strategy, as agreed while defining rules ----
SWING_WIDTH_1M = 1   # "newyork" / "newyork tt": 1 candle each side on 1M
SWING_WIDTH_5M = 1   # "Asian tt": 1 candle each side on 5M

# ---- Pullback / structure thresholds ----
LONDON_TT_MIN_PULLBACK_PCT = 0.25   # "London tt": >=25% of Asian range
LONDON_TT_WEAK_BODY_PCT = 0.30      # "London tt": weak candle body < 30% of range

# ---- State file (tracks which setups have already been alerted, so we
# don't spam the same signal every run) ----
STATE_FILE = "state.json"

# ---- Live setups file (written every run for the dashboard) ----
LIVE_SETUPS_FILE = "live_setups.json"

# ---- Partial-confluence notifications ----
# Notify when a setup is missing at most this many steps out of its full
# checklist (e.g. 1 = "4 out of 5 confirmed, one step left").
NEAR_MISS_MAX_MISSING = 1

# ---- News pause ----
# Skip sending full alerts (not progress heads-ups) within this many
# minutes of a high-impact news event for a watched currency. Uses a
# free, no-key calendar feed - if it can't be reached, alerts send as
# normal rather than blocking on a missing calendar.
NEWS_PAUSE_ENABLED = True
NEWS_PAUSE_BUFFER_MINUTES = 30
NEWS_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
WATCHED_NEWS_CURRENCIES = ["USD"]

# ---- Fundamentals feature ----
# Which currencies' economic events matter for each symbol's fundamentals.
FUNDAMENTALS_CURRENCIES_BY_SYMBOL = {
    "NAS100": ["USD"],
    "XAUUSD": ["USD"],
    "EURUSD": ["USD", "EUR"],
}
# Currencies covered by the once-daily proactive digest message.
DAILY_DIGEST_CURRENCIES = ["USD", "EUR"]

# ---- Outcome logging (for later backtesting/review) ----
ALERTS_LOG_FILE = "alerts_log.json"
ALERTS_LOG_MAX_ENTRIES = 2000

# ---- Twelve Data free-tier budget ----
# Free plan: 8 credits/minute, 800 credits/day, 1 credit per symbol per
# request. main.py trims which symbol/timeframe combos it fetches based
# on which killzone is currently relevant, to stay comfortably under
# this - check usage on your Twelve Data dashboard for the first few
# days and tell me if it's running close to the cap.
TWELVEDATA_DAILY_CREDIT_BUDGET = 800

# ---- Dashboard-controlled settings (settings.json, written by the web
# dashboard via GitHub's API - overrides the defaults above if present).
# Absence of the file is normal (nothing's been changed from the
# dashboard yet) - defaults apply. Placed last so it can override
# anything defined earlier in this file. ----
import json as _json

SETTINGS_FILE = "settings.json"
if os.path.exists(SETTINGS_FILE):
    try:
        with open(SETTINGS_FILE) as _f:
            _settings = _json.load(_f)
        RISK_PERCENT = _settings.get("risk_percent", RISK_PERCENT)
        ACCOUNT_BALANCE = _settings.get("account_balance", ACCOUNT_BALANCE)
        NEWS_PAUSE_ENABLED = _settings.get("news_pause_enabled", NEWS_PAUSE_ENABLED)
        _symbols_enabled = _settings.get("symbols_enabled", {})
        # A symbol with no explicit "false" in settings.json stays enabled.
        ALL_SYMBOLS = [s for s in ALL_SYMBOLS if _symbols_enabled.get(s, True)]
    except (_json.JSONDecodeError, OSError) as _exc:
        print(f"[config] could not read settings.json, using defaults: {_exc}")
