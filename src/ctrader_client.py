"""
cTrader Open API client - used ONLY for NAS100 now, since Twelve Data's
free plan doesn't include major US indices (confirmed empty result on
both NDX and IXIC). Gold and EURUSD stay on Twelve Data, which works
fine for them. This keeps the OAuth complexity scoped to just the one
symbol that actually needs it.

Much simpler than the original version: no account balance or symbol
spec fetching needed here (position sizing uses config.ACCOUNT_BALANCE
and config.ASSUMED_LOT_SIZE regardless of data source) - this only
fetches trendbars.
"""

import time as time_module
from datetime import datetime, timezone

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

COUNT_PER_PERIOD = {
    "1M": 240, "5M": 200, "15M": 150, "30M": 120, "1H": 100,
}

LOOKBACK_MINUTES = {
    "1M": 6 * 60, "5M": 24 * 60, "15M": 3 * 24 * 60,
    "30M": 5 * 24 * 60, "1H": 10 * 24 * 60,
}


def refresh_access_token() -> str:
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


def fetch_nas100_trendbars(period_labels: list) -> dict:
    """Returns {("NAS100", period_label): [Candle, ...], ...} for the
    requested periods. Empty dict on any failure (never crashes the
    whole run - main.py just won't have NAS100 data that cycle)."""
    if not period_labels:
        return {}

    try:
        access_token = refresh_access_token()
    except Exception as exc:  # noqa: BLE001
        print(f"[ctrader] token refresh failed: {exc}")
        return {}

    results = {}
    host = EndPoints.PROTOBUF_LIVE_HOST if config.CTRADER_IS_LIVE else EndPoints.PROTOBUF_DEMO_HOST
    client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)
    done = defer.Deferred()

    def on_error(failure):
        print(f"[ctrader] error: {failure}")
        if not done.called:
            done.errback(failure)

    def fetch_next(remaining):
        if not remaining:
            client.setDisconnectedCallback(lambda *a: None)
            client.stopService()
            if not done.called:
                done.callback(results)
            return

        period_label = remaining[0]
        req = ProtoOAGetTrendbarsReq()
        req.ctidTraderAccountId = config.CTRADER_ACCOUNT_ID
        req.symbolId = config.CTRADER_SYMBOL_ID_NAS100
        req.period = PERIOD_MAP[period_label]
        req.count = COUNT_PER_PERIOD[period_label]
        to_ts = int(time_module.time() * 1000)
        req.toTimestamp = to_ts
        req.fromTimestamp = to_ts - LOOKBACK_MINUTES[period_label] * 60 * 1000

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
            results[("NAS100", period_label)] = candles
            fetch_next(remaining[1:])

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
            d2.addCallbacks(lambda _r: fetch_next(period_labels), on_error)

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
    try:
        reactor.run()
    except Exception as exc:  # noqa: BLE001
        print(f"[ctrader] reactor error: {exc}")
        return {}

    return results
