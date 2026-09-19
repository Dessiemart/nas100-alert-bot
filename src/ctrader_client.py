"""
cTrader Open API client. Fetches all three symbols (NAS100, XAUUSD,
EURUSD) over one connection per run - Twelve Data is no longer used
for market data at all.

UPDATED: added 4H and Daily period support (H4, D1), used for the
NAS100 snapshot file that lets the Apps Script AI-chat feature analyze
NAS100 too, since cTrader needs a persistent connection Apps Script
can't hold open itself.

Refresh-token rotation: cTrader hands back a new refresh token every
time the old one is used, so the new access token AND new refresh
token are both pushed back to GitHub Secrets automatically after every
refresh.
"""

import os
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
from src import secret_updater

TOKEN_URL = "https://openapi.ctrader.com/apps/token"

GITHUB_OWNER = "Dessiemart"
GITHUB_REPO = "nas100-alert-bot"

SYMBOL_ID_MAP = {
    "NAS100": config.CTRADER_SYMBOL_ID_NAS100,
    "XAUUSD": config.CTRADER_SYMBOL_ID_XAUUSD,
    "EURUSD": config.CTRADER_SYMBOL_ID_EURUSD,
}

PERIOD_MAP = {
    "1M": ProtoOATrendbarPeriod.M1,
    "5M": ProtoOATrendbarPeriod.M5,
    "15M": ProtoOATrendbarPeriod.M15,
    "30M": ProtoOATrendbarPeriod.M30,
    "1H": ProtoOATrendbarPeriod.H1,
    "4H": ProtoOATrendbarPeriod.H4,
    "1D": ProtoOATrendbarPeriod.D1,
}

COUNT_PER_PERIOD = {
    "1M": 240, "5M": 200, "15M": 150, "30M": 120, "1H": 100, "4H": 100, "1D": 100,
}

LOOKBACK_MINUTES = {
    "1M": 6 * 60, "5M": 24 * 60, "15M": 3 * 24 * 60,
    "30M": 5 * 24 * 60, "1H": 10 * 24 * 60,
    "4H": 40 * 24 * 60, "1D": 150 * 24 * 60,
}


def _summary(line: str):
    """Appends a line to the GitHub Actions Job Summary tab, if running
    in Actions. No-op (just prints) if run locally/elsewhere."""
    print(line)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass


def refresh_access_token() -> str:
    """Exchanges the current refresh token for a fresh access token,
    then immediately pushes BOTH the new access token and the new
    (rotated) refresh token back to GitHub Secrets so next run has a
    working refresh token too."""
    _summary("### cTrader token refresh")
    try:
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
    except Exception as exc:  # noqa: BLE001
        _summary(f"🔴 Refresh HTTP call FAILED: {exc}")
        raise

    data = resp.json()
    access_token = data.get("accessToken") or data.get("access_token")
    new_refresh_token = data.get("refreshToken") or data.get("refresh_token")

    if not access_token:
        _summary(f"🔴 Refresh call succeeded but no access token in response: {data}")
        raise RuntimeError(f"Unexpected token response: {data}")

    _summary("✅ Access token refreshed successfully.")

    if new_refresh_token and new_refresh_token != config.CTRADER_REFRESH_TOKEN:
        _summary("ℹ️ Refresh token rotated - attempting to persist new tokens to GitHub Secrets...")
        gh_pat = os.environ.get("GH_PAT_FOR_SECRETS")
        if not gh_pat:
            _summary("🔴 GH_PAT_FOR_SECRETS is not set - cannot persist. "
                      "NEXT run will fail unless you update the secret manually.")
        else:
            try:
                secret_updater.update_repo_secret(
                    GITHUB_OWNER, GITHUB_REPO, gh_pat, "CTRADER_ACCESS_TOKEN", access_token,
                )
                secret_updater.update_repo_secret(
                    GITHUB_OWNER, GITHUB_REPO, gh_pat, "CTRADER_REFRESH_TOKEN", new_refresh_token,
                )
                _summary("✅ Rotated tokens persisted to GitHub Secrets successfully.")
            except Exception as exc:  # noqa: BLE001
                _summary(f"🔴 Failed to persist rotated tokens: {exc}")
                _summary("NEXT run will fail unless you update CTRADER_ACCESS_TOKEN/"
                          "CTRADER_REFRESH_TOKEN secrets manually with the values from this run.")
    else:
        _summary("ℹ️ Refresh token did not change this run - nothing to persist.")

    return access_token


def fetch_trendbars(symbol_period_list: list) -> dict:
    """symbol_period_list: [("NAS100", "1H"), ("XAUUSD", "5M"), ...]
    Returns {(symbol, period): [Candle, ...], ...} for every requested
    combo that succeeded. Missing/failed combos are simply absent from
    the result (never crashes the whole run)."""
    if not symbol_period_list:
        return {}

    try:
        access_token = refresh_access_token()
    except Exception as exc:  # noqa: BLE001
        _summary(f"🔴 Fetch ABORTED - token refresh failed: {exc}")
        return {}

    results = {}
    host = EndPoints.PROTOBUF_LIVE_HOST if config.CTRADER_IS_LIVE else EndPoints.PROTOBUF_DEMO_HOST
    client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)
    done = defer.Deferred()

    def on_error(failure):
        _summary(f"🔴 cTrader connection error: {failure}")
        if not done.called:
            done.errback(failure)

    def fetch_next(remaining):
        if not remaining:
            client.setDisconnectedCallback(lambda *a: None)
            client.stopService()
            if not done.called:
                done.callback(results)
            return

        symbol, period_label = remaining[0]
        symbol_id = SYMBOL_ID_MAP.get(symbol)
        if symbol_id is None:
            _summary(f"🔴 No cTrader symbol ID configured for {symbol} - skipping.")
            fetch_next(remaining[1:])
            return

        req = ProtoOAGetTrendbarsReq()
        req.ctidTraderAccountId = config.CTRADER_ACCOUNT_ID
        req.symbolId = symbol_id
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
            results[(symbol, period_label)] = candles
            _summary(f"✅ {symbol} {period_label}: {len(candles)} candles")
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
            d2.addCallbacks(lambda _r: fetch_next(symbol_period_list), on_error)

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
        _summary(f"🔴 fetch reactor error: {exc}")
        return {}

    return results
