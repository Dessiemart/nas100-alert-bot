"""
One-time discovery script.
Connects to cTrader's Open API over TCP, lists your trading accounts,
authorizes the first (demo) account, then lists its symbols and looks
for anything matching NAS100, Gold (XAUUSD), or EUR/USD.

UPDATED: now also searches for Gold/EUR (not just NAS100), and instead
of only printing to the Actions log, it sends the results straight to
your Telegram - no log-reading needed.

Run this ONCE via GitHub Actions (workflow_dispatch). Has a built-in
30-second timeout so it can never hang forever.
"""
import os
import sys

import requests
from twisted.internet import reactor
from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAApplicationAuthReq,
    ProtoOAGetAccountListByAccessTokenReq,
    ProtoOAAccountAuthReq,
    ProtoOASymbolsListReq,
)

CLIENT_ID = os.environ["CTRADER_CLIENT_ID"]
CLIENT_SECRET = os.environ["CTRADER_CLIENT_SECRET"]
ACCESS_TOKEN = os.environ["CTRADER_ACCESS_TOKEN"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

KEYWORD_GROUPS = {
    "NAS100": ("NAS100", "US TECH 100", "USTECH100", "NASDAQ 100", "US100"),
    "XAUUSD (Gold)": ("XAU",),
    "EURUSD": ("EUR/USD", "EURUSD"),
}

client = Client(EndPoints.PROTOBUF_DEMO_HOST, EndPoints.PROTOBUF_PORT, TcpProtocol)

timeout_call = None


def send_telegram(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
    except Exception as e:
        print(f"Telegram send failed (results are still below in the log): {e}")


def stop_reactor():
    if timeout_call is not None and timeout_call.active():
        timeout_call.cancel()
    if reactor.running:
        reactor.stop()


def on_timeout():
    msg = "🔴 Symbol discovery TIMED OUT after 30s - no response from cTrader server. Check CLIENT_ID/CLIENT_SECRET/ACCESS_TOKEN secrets."
    print(msg)
    send_telegram(msg)
    if reactor.running:
        reactor.stop()


def on_error(failure):
    msg = f"🔴 Symbol discovery ERROR: {failure}"
    print(msg)
    send_telegram(msg)
    stop_reactor()


def on_connected(_client):
    print("Connected. Authenticating application...")
    request = ProtoOAApplicationAuthReq()
    request.clientId = CLIENT_ID
    request.clientSecret = CLIENT_SECRET
    deferred = client.send(request)
    deferred.addCallbacks(on_app_auth, on_error)


def on_app_auth(result):
    response = Protobuf.extract(result)
    if response.__class__.__name__ == "ProtoOAErrorRes":
        msg = f"🔴 APP AUTH REJECTED: {response.errorCode} - {response.description}"
        print(msg)
        send_telegram(msg)
        stop_reactor()
        return
    print("App authenticated. Fetching account list...")
    request = ProtoOAGetAccountListByAccessTokenReq()
    request.accessToken = ACCESS_TOKEN
    deferred = client.send(request)
    deferred.addCallbacks(on_accounts, on_error)


def on_accounts(result):
    response = Protobuf.extract(result)
    if response.__class__.__name__ == "ProtoOAErrorRes":
        msg = f"🔴 SERVER REJECTED THE REQUEST: {response.errorCode} - {response.description}"
        print(msg)
        send_telegram(msg)
        stop_reactor()
        return
    accounts = list(response.ctidTraderAccount)
    if not accounts:
        msg = "🔴 No accounts found for this access token."
        print(msg)
        send_telegram(msg)
        stop_reactor()
        return

    target = next((a for a in accounts if not a.isLive), accounts[0])
    print(f"Using account {target.ctidTraderAccountId} to look up symbols...")

    request = ProtoOAAccountAuthReq()
    request.ctidTraderAccountId = target.ctidTraderAccountId
    request.accessToken = ACCESS_TOKEN
    deferred = client.send(request)
    deferred.addCallbacks(lambda r: on_account_auth(r, target.ctidTraderAccountId), on_error)


def on_account_auth(result, account_id):
    response = Protobuf.extract(result)
    if response.__class__.__name__ == "ProtoOAErrorRes":
        msg = f"🔴 ACCOUNT AUTH REJECTED: {response.errorCode} - {response.description}"
        print(msg)
        send_telegram(msg)
        stop_reactor()
        return
    print("Account authorized. Fetching symbol list (this can take a few seconds)...")
    request = ProtoOASymbolsListReq()
    request.ctidTraderAccountId = account_id
    deferred = client.send(request)
    deferred.addCallbacks(on_symbols, on_error)


def on_symbols(result):
    response = Protobuf.extract(result)
    all_symbols = [(sym.symbolId, sym.symbolName) for sym in response.symbol]
    print(f"Total symbols on this account: {len(all_symbols)}")

    lines = [f"<b>cTrader Symbol Discovery</b> ({len(all_symbols)} total symbols)\n"]
    for label, keywords in KEYWORD_GROUPS.items():
        matches = [
            (sid, name) for sid, name in all_symbols
            if any(kw in name.upper() for kw in keywords)
        ]
        lines.append(f"<b>{label}:</b>")
        if matches:
            for sid, name in matches:
                lines.append(f"  id={sid}  name=\"{name}\"")
        else:
            lines.append("  (no match found)")
        lines.append("")

    message = "\n".join(lines)
    print(message)
    send_telegram(message)
    print("\nDONE - check Telegram for the results.")
    stop_reactor()


def on_disconnected(_client, reason):
    print(f"Disconnected: {reason}")


client.setConnectedCallback(on_connected)
client.setDisconnectedCallback(on_disconnected)
client.startService()

timeout_call = reactor.callLater(30, on_timeout)
reactor.run()
