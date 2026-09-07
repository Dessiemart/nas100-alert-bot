"""
Central configuration for the alert engine.

Everything sensitive comes from environment variables, which GitHub
Actions injects from your repository Secrets. Nothing here is a real
credential - it's all `os.environ[...]` lookups.

Required GitHub Secrets (Settings -> Secrets and variables -> Actions):
  TELEGRAM_BOT_TOKEN     - from BotFather
  TELEGRAM_CHAT_ID       - your Telegram chat id
  TWELVEDATA_API_KEY     - free key from twelvedata.com/register (no card,
                           no approval wait - instant)
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

# ---- Twelve Data ----
TWELVEDATA_API_KEY = _env("TWELVEDATA_API_KEY")

# Our internal symbol names -> Twelve Data's symbol strings.
# FLAG FOR VERIFICATION: "NDX" is the standard Nasdaq-100 ticker used by
# most data providers - very likely correct on Twelve Data too, but
# worth a one-off test once you have your API key (see README).
TWELVEDATA_SYMBOLS = {
    "NAS100": "NDX",
    "XAUUSD": "XAU/USD",
    "EURUSD": "EUR/USD",
}

# ---- Account balance for position sizing ----
# Twelve Data is market data only - it has no concept of your broker
# account, so this is a plain number you maintain yourself (a future
# bot command will let you update it without editing code).
ACCOUNT_BALANCE = 1000.0

# ---- Assumed contract sizes (units per 1.0 lot) for position sizing ----
# FLAG FOR VERIFICATION: these are industry-standard assumptions
# (EURUSD 100,000 units/lot, XAUUSD 100 oz/lot are essentially universal;
# NAS100 CFD contract size varies by broker - 1.0 here means "1 unit of
# index per lot", i.e. $1 per point per lot, which is common but NOT
# universal). Check your broker's actual contract specification page and
# adjust NAS100 below if it differs, or position sizes will be wrong.
ASSUMED_LOT_SIZE = {
    "NAS100": 1.0,
    "XAUUSD": 100.0,
    "EURUSD": 100000.0,
}
RISK_PERCENT = 0.05

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
