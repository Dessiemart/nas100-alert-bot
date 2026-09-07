"""
Twelve Data client - replaces the old cTrader connection entirely.

Much simpler than the cTrader version: plain HTTP requests, no OAuth, no
persistent socket, no Twisted reactor. Twelve Data's free tier is 8
credits/minute and 800 credits/day (1 credit per symbol per request) -
main.py decides which symbol/timeframe combos are worth fetching each
run to stay under that; this module just does the fetching.

Returns the same shape main.py already expects from the old cTrader
client - trendbars dict, plus "balance" and "symbols" (SymbolSpec) built
from config instead of fetched live, since Twelve Data has no concept of
a broker account.
"""

from datetime import datetime, timezone

import requests

import config
from src.candles import Candle
from src.position_sizing import SymbolSpec

BASE_URL = "https://api.twelvedata.com/time_series"

INTERVAL_MAP = {
    "1M": "1min",
    "5M": "5min",
    "15M": "15min",
    "30M": "30min",
    "1H": "1h",
}

# How many candles back to request per timeframe.
OUTPUT_SIZE = {
    "1M": 240,
    "5M": 200,
    "15M": 150,
    "30M": 120,
    "1H": 100,
}


def fetch_candles(symbol_name: str, period_label: str) -> list[Candle]:
    td_symbol = config.TWELVEDATA_SYMBOLS[symbol_name]
    interval = INTERVAL_MAP[period_label]
    outputsize = OUTPUT_SIZE[period_label]

    resp = requests.get(
        BASE_URL,
        params={
            "symbol": td_symbol,
            "interval": interval,
            "outputsize": outputsize,
            "timezone": "UTC",
            "apikey": config.TWELVEDATA_API_KEY,
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") == "error":
        raise RuntimeError(f"Twelve Data error for {symbol_name} {period_label}: {data.get('message')}")

    values = data.get("values", [])
    candles = []
    for row in values:
        dt = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) \
            if len(row["datetime"]) > 10 else \
            datetime.strptime(row["datetime"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        candles.append(Candle(
            time=dt,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
        ))
    # Twelve Data returns most-recent-first - flip to oldest -> newest.
    candles.sort(key=lambda c: c.time)
    return candles


def fetch_market_data(requests_wanted: list[tuple[str, str]]) -> dict:
    """
    requests_wanted: list of (symbol_name, period_label) pairs.

    Returns the same shape the old cTrader client returned, so main.py
    and everything downstream (strategies, confluence, position sizing)
    needs no changes beyond the import line:
        {
            "trendbars": {(symbol, period): [Candle, ...], ...},
            "balance": config.ACCOUNT_BALANCE,
            "symbols": {symbol_name: SymbolSpec, ...},
        }
    """
    trendbars = {}
    for symbol_name, period_label in requests_wanted:
        try:
            trendbars[(symbol_name, period_label)] = fetch_candles(symbol_name, period_label)
        except Exception as exc:  # noqa: BLE001 - one bad fetch shouldn't kill the whole run
            print(f"[twelvedata] failed to fetch {symbol_name} {period_label}: {exc}")

    symbol_specs = {
        name: SymbolSpec(
            lot_size=config.ASSUMED_LOT_SIZE[name],
            min_volume=1,       # 0.01 lot, in centilots
            max_volume=10000000,  # generous cap; broker's real limit may be lower
            step_volume=1,       # 0.01 lot steps
        )
        for name in config.TWELVEDATA_SYMBOLS
    }

    return {
        "trendbars": trendbars,
        "balance": config.ACCOUNT_BALANCE,
        "symbols": symbol_specs,
    }
