"""
One-time discovery script.
Connects to cTrader's Open API over TCP, lists your trading accounts,
authorizes the first (demo) account, then lists its symbols and prints
the symbolId for anything matching NAS100 / US Tech 100 / NASDAQ 100.

Run this ONCE via GitHub Actions (workflow_dispatch), read the printed
values from the Actions log, then delete this script and the workflow
file - it is not part of the permanent bot.

Has a built-in 30-second timeout so it can never hang forever.
"""
import os
import sys

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

NAS100_KEYWORDS = ("NAS100", "US TECH 100", "USTECH100", "NASDAQ 100", "US100")

client = Client(EndPoints.PROTOBUF_DEMO_HOST, EndPoints.PROTOBUF_PORT, TcpProtocol)

timeout_call = None  # set once reactor starts


def stop_reactor():
    if timeout_call is not None and timeout_call.active():
        timeout_call.cancel()
    if reactor.running:
        reactor.stop()


def on_timeout():
    print("\nTIMED OUT after 30 seconds - no response from cTrader server.")
    print("This usually means a request was sent but no reply ever arrived.")
    print("Check that CLIENT_ID, CLIENT_SECRET and ACCESS_TOKEN secrets are correct.")
    if reactor.running:
        reactor.stop()


def on_error(failure):
    print(f"ERROR: {failure}")
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
        print(f"\nAPP AUTH REJECTED:")
        print(f"  errorCode = {response.errorCode}")
        print(f"  description = {response.description}")
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
        print(f"\nSERVER REJECTED THE REQUEST:")
        print(f"  errorCode = {response.errorCode}")
        print(f"  description = {response.description}")
        stop_reactor()
        return
    accounts = list(response.ctidTraderAccount)
    if not accounts:
        print("No accounts found for this access token.")
        stop_reactor()
        return

    print("\n=== ACCOUNTS FOUND ===")
    for acc in accounts:
        kind = "LIVE" if acc.isLive else "DEMO"
        print(f"  ctidTraderAccountId = {acc.ctidTraderAccountId}   ({kind})")
    print("======================\n")

    # Prefer a demo account (matches the user's Pepperstone demo setup)
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
        print(f"\nACCOUNT AUTH REJECTED:")
        print(f"  errorCode = {response.errorCode}")
        print(f"  description = {response.description}")
        stop_reactor()
        return
    print("Account authorized. Fetching symbol list (this can take a few seconds)...")
    request = ProtoOASymbolsListReq()
    request.ctidTraderAccountId = account_id
    deferred = client.send(request)
    deferred.addCallbacks(on_symbols, on_error)


def on_symbols(result):
    response = Protobuf.extract(result)
    print(f"\nTotal symbols on this account: {len(response.symbol)}")
    print("\n=== POSSIBLE NAS100 MATCHES ===")
    found_any = False
    for sym in response.symbol:
        name_upper = sym.symbolName.upper()
        if any(kw in name_upper for kw in NAS100_KEYWORDS):
            found_any = True
            print(f"  symbolId = {sym.symbolId}   name = \"{sym.symbolName}\"")
    if not found_any:
        print("  No obvious match found. Printing ALL symbol names instead:")
        for sym in response.symbol:
            print(f"    symbolId = {sym.symbolId}   name = \"{sym.symbolName}\"")
    print("================================\n")
    print("DONE. Copy the correct ctidTraderAccountId and symbolId values above.")
    stop_reactor()


def on_disconnected(_client, reason):
    print(f"Disconnected: {reason}")


client.setConnectedCallback(on_connected)
client.setDisconnectedCallback(on_disconnected)
client.startService()

timeout_call = reactor.callLater(30, on_timeout)
reactor.run()
