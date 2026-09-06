"""
Thin wrapper around Spotware's official `ctrader-open-api` (OpenApiPy)
package. Opens one short-lived connection per script run: authenticates
the app + your account, fetches the trendbars (candles) each strategy
needs for this cycle, then disconnects. This matches the "closed-candle
polling, no always-open connection" design decided earlier.

Access tokens expire after ~30 days; we don't store an access token at
all - every run exchanges the long-lived refresh token for a fresh
access token first (one HTTP call), then uses that for the socket
session. The refresh token itself never expires.
"""

import time as time_module
from datetime import datetime, timezone, timedelta

import requests
from twisted.internet import reactor, defer
from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAApplicationAuthReq,
    ProtoOAAccountAuthReq,
    ProtoOAGetTrendbarsReq,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOATrendbarPeriod

import config
from src.candles import Candle, raw_trendbar_to_candle

TOKEN_URL = "https://openapi.ctrader.com/apps/token"

PERIOD_MAP = {
    "1M": ProtoOATrendbarPeriod.M1,
    "5M": ProtoOATrendbarPeriod.M5,
    "15M": ProtoOATrendbarPeriod.M15,
    "30M": ProtoOATrendbarPeriod.M30,
    "1H": ProtoOATrendbarPeriod.H1,
}

# How many candles back to request per timeframe - generous enough to cover
# a full session's worth of structure without over-fetching.
COUNT_PER_PERIOD = {
    "1M": 240,
    "5M": 200,
    "15M": 150,
    "30M": 120,
    "1H": 100,
}


def refresh_access_token() -> str:
    """Exchange the long-lived refresh token for a fresh access token."""
    resp = requests.get(
        TOKEN_URL,
        params={
            "grant_type": "refresh_token",
            "refresh_token": config.CTRADER_REFRESH_TOKEN,
            "client_id": config.CTRADER_CLIENT_ID,
            "client_secret": config.CTRADER_CLIENT_SECRET,
        },
        headers={"Accept": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if "accessToken" not in data and "access_token" not in data:
        raise RuntimeError(f"Unexpected token response: {data}")
    return data.get("accessToken") or data.get("access_token")


def fetch_all_trendbars(requests_wanted: list[tuple[str, str]]) -> dict[tuple[str, str], list[Candle]]:
    """
    requests_wanted: list of (symbol_name, period_label) pairs, e.g.
        [("NAS100", "5M"), ("NAS100", "1H"), ("XAUUSD", "15M")]

    Returns a dict keyed by the same tuples, each holding a list of
    Candle objects ordered oldest -> newest.
    """
    access_token = refresh_access_token()
    results: dict[tuple[str, str], list[Candle]] = {}

    host = EndPoints.PROTOBUF_LIVE_HOST if config.CTRADER_IS_LIVE else EndPoints.PROTOBUF_DEMO_HOST
    client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)

    done = defer.Deferred()

    def on_error(failure):
        print(f"[ctrader] error: {failure}")
        if not done.called:
            done.errback(failure)

    def request_next(remaining):
        if not remaining:
            client.setDisconnectedCallback(lambda *a: None)
            client.stopService()
            if not done.called:
                done.callback(results)
            return

        symbol_name, period_label = remaining[0]
        symbol_id = config.SYMBOL_IDS[symbol_name]
        period = PERIOD_MAP[period_label]
        count = COUNT_PER_PERIOD[period_label]

        req = ProtoOAGetTrendbarsReq()
        req.ctidTraderAccountId = config.CTRADER_ACCOUNT_ID
        req.symbolId = symbol_id
        req.period = period
        req.count = count
        to_ts = int(time_module.time() * 1000)
        req.toTimestamp = to_ts
        # fromTimestamp is required by some server versions even with count set;
        # give it a generous lookback window per period.
        lookback_minutes = {
            "1M": 6 * 60, "5M": 24 * 60, "15M": 3 * 24 * 60,
            "30M": 5 * 24 * 60, "1H": 10 * 24 * 60,
        }[period_label]
        req.fromTimestamp = to_ts - lookback_minutes * 60 * 1000

        deferred = client.send(req)

        def on_response(response):
            bars_msg = Protobuf.extract(response)
            candles = []
            for bar in bars_msg.trendbar:
                open_time = datetime.fromtimestamp(bar.utcTimestampInMinutes * 60, tz=timezone.utc)
                candles.append(raw_trendbar_to_candle(
                    open_time, bar.low, bar.deltaOpen, bar.deltaHigh, bar.deltaClose,
                ))
            candles.sort(key=lambda c: c.time)
            results[(symbol_name, period_label)] = candles
            request_next(remaining[1:])

        deferred.addCallbacks(on_response, on_error)

    def connected(_client):
        auth_req = ProtoOAApplicationAuthReq()
        auth_req.clientId = config.CTRADER_CLIENT_ID
        auth_req.clientSecret = config.CTRADER_CLIENT_SECRET
        d = client.send(auth_req)

        def app_authed(_response):
            acc_req = ProtoOAAccountAuthReq()
            acc_req.ctidTraderAccountId = config.CTRADER_ACCOUNT_ID
            acc_req.accessToken = access_token
            d2 = client.send(acc_req)
            d2.addCallbacks(lambda _r: request_next(requests_wanted), on_error)

        d.addCallbacks(app_authed, on_error)

    def disconnected(_client, reason):
        print(f"[ctrader] disconnected: {reason}")

    client.setConnectedCallback(connected)
    client.setDisconnectedCallback(disconnected)
    client.startService()

    def stop_reactor(_result):
        if reactor.running:
            reactor.stop()
        return _result

    def stop_reactor_err(failure):
        if reactor.running:
            reactor.stop()
        return failure

    done.addCallbacks(stop_reactor, stop_reactor_err)
    reactor.run()

    return results
