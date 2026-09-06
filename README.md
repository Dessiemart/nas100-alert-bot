# NAS100 / Gold / EURUSD Alert Bot

Alert-only market-structure engine. Runs on GitHub Actions every 5
minutes, reads candles from cTrader, checks 5 mechanical strategies, and
sends a Telegram message when one confirms. It never places trades.

## Files

- `config.py` - loads all settings from GitHub Secrets / env vars
- `src/killzones.py` - ICT killzone session windows (Asian/London/NY AM/NY PM)
- `src/candles.py` - swing points, FVG, IFVG, order block, structure helpers
- `src/strategies.py` - the 5 strategies: Asians, London tt, Asian tt, newyork, newyork tt
- `src/ctrader_client.py` - connects to cTrader, fetches candles
- `src/telegram_alert.py` - sends the Telegram message
- `main.py` - entry point, run by the workflow
- `.github/workflows/alert.yml` - the schedule
- `state.json` - auto-created; tracks which setups were already alerted

## One-time setup (do this after the cTrader app shows "Active")

You already have `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` saved. Six
more secrets are needed. All of the steps below are just visiting URLs
in your phone's browser - the same trick you used for the Telegram chat
ID - no coding tool required.

### Step 1 - Get an authorization code

Visit this URL (replace `CLIENT_ID` with your MarketBot Client ID):

```
https://openapi.ctrader.com/apps/auth?client_id=CLIENT_ID&redirect_uri=https://dessiemart.com.et&scope=accounts
```

Log in with your cTrader ID, approve access, and you'll be redirected to
`https://dessiemart.com.et/?code=...`. Copy the `code` value from the
address bar (everything after `code=`).

⚠️ This code expires quickly (a few minutes) - do steps 2 immediately after.

### Step 2 - Exchange the code for tokens

Visit this URL (fill in all four placeholders):

```
https://openapi.ctrader.com/apps/token?grant_type=authorization_code&code=YOUR_CODE&redirect_uri=https://dessiemart.com.et&client_id=CLIENT_ID&client_secret=CLIENT_SECRET
```

You'll get back JSON containing `"accessToken"` and `"refreshToken"`.
**Save the refreshToken** - that's your `CTRADER_REFRESH_TOKEN` secret.
The accessToken isn't needed here; the bot fetches a fresh one every run.

### Step 3 - Get your account ID

Visit (using the accessToken from step 2):

```
https://api.spotware.com/connect/tradingaccounts?access_token=YOUR_ACCESS_TOKEN
```

Find `"accountId"` in the response - that's your `CTRADER_ACCOUNT_ID`
secret (a plain number, different from your login number 5338067).

### Step 4 - Get symbol IDs

Visit (using your accountId from step 3):

```
https://api.spotware.com/connect/tradingaccounts/YOUR_ACCOUNT_ID/symbols?access_token=YOUR_ACCESS_TOKEN
```

This returns a long list of symbols. Search the page (browser's
find-in-page) for `"US Tech 100"`, `"Gold"`, and `"EURUSD"` and note
each one's `"symbolId"` number. Those become `SYMBOL_ID_NAS100`,
`SYMBOL_ID_XAUUSD`, `SYMBOL_ID_EURUSD`.

### Step 5 - Add all six secrets to GitHub

Same place as before (Settings -> Secrets and variables -> Actions ->
New repository secret):

| Name | Value |
|---|---|
| `CTRADER_CLIENT_ID` | your MarketBot Client ID |
| `CTRADER_CLIENT_SECRET` | your MarketBot Client Secret |
| `CTRADER_REFRESH_TOKEN` | from Step 2 |
| `CTRADER_ACCOUNT_ID` | from Step 3 |
| `SYMBOL_ID_NAS100` | from Step 4 |
| `SYMBOL_ID_XAUUSD` | from Step 4 |
| `SYMBOL_ID_EURUSD` | from Step 4 |

(`CTRADER_IS_LIVE` is optional - leave it unset, it defaults to demo.)

### Step 6 - Enable the workflow and test it

1. Add all the code files below to your repo (paths shown match what to
   type in GitHub's "create new file" box).
2. Go to the **Actions** tab -> **NAS100 Alert Bot** -> **Run workflow**
   (this is the `workflow_dispatch` trigger, so you don't have to wait
   for the schedule).
3. Watch the run - tap into it and check the "Run alert engine" step
   log for errors.
4. Once it runs clean, it will fire automatically every 5 minutes from
   then on.

## Known v1 limitations (flagging, not hiding)

- **"Asians" strategy has no explicit SL/TP rule** in our definitions -
  only the entry trigger was formalized. The code defaults to SL beyond
  the triggering zone and TP at 2R, clearly marked in the alert message.
  Tell me if you want different handling.
- Structure/FVG/OB detection is a first-pass mechanical translation of
  the rules, not yet backtested (that's Stage 13 in the plan) - treat
  early alerts as something to sanity-check against the chart before
  trusting them for real entries.
