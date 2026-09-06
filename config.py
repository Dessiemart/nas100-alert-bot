"""
Central configuration for the alert engine.

Everything sensitive comes from environment variables, which GitHub
Actions injects from your repository Secrets. Nothing here is a real
credential - it's all `os.environ[...]` lookups.

Required GitHub Secrets (Settings -> Secrets and variables -> Actions):
  TELEGRAM_BOT_TOKEN     - from BotFather
  TELEGRAM_CHAT_ID       - your Telegram chat id (already set)
  CTRADER_CLIENT_ID      - MarketBot app Client ID
  CTRADER_CLIENT_SECRET  - MarketBot app Client Secret
  CTRADER_REFRESH_TOKEN  - obtained once via the browser-URL flow in README.md
  CTRADER_ACCOUNT_ID     - your ctidTraderAccountId (a number, NOT your
                           login number) - also obtained via README.md
  SYMBOL_ID_NAS100       - numeric symbolId for "US Tech 100 Index"
  SYMBOL_ID_XAUUSD       - numeric symbolId for Gold
  SYMBOL_ID_EURUSD       - numeric symbolId for EUR/USD
  CTRADER_IS_LIVE        - "true" or "false" (optional, defaults to false
                           since you're on a demo account)
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


# ---- cTrader app + account credentials ----
CTRADER_CLIENT_ID = _env("CTRADER_CLIENT_ID")
CTRADER_CLIENT_SECRET = _env("CTRADER_CLIENT_SECRET")
CTRADER_REFRESH_TOKEN = _env("CTRADER_REFRESH_TOKEN")
CTRADER_ACCOUNT_ID = int(_env("CTRADER_ACCOUNT_ID"))
CTRADER_IS_LIVE = _env("CTRADER_IS_LIVE", required=False, default="false").lower() == "true"

# ---- Telegram ----
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID")

# ---- Symbol IDs on your broker (Pepperstone demo #5338067) ----
SYMBOL_IDS = {
    "NAS100": int(_env("SYMBOL_ID_NAS100")),
    "XAUUSD": int(_env("SYMBOL_ID_XAUUSD")),
    "EURUSD": int(_env("SYMBOL_ID_EURUSD")),
}
SYMBOL_ID_TO_NAME = {v: k for k, v in SYMBOL_IDS.items()}

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
