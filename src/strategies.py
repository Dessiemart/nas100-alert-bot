"""
SMC strategy modules merged from the "Trading Strategy Coding Spec" PDF,
REVISED for high setup frequency.

Design change vs the first version: every module now alerts at MULTIPLE
stages of its chain, not only when every confirmation is present:

  Stage 1 (anticipation):  pending stop/limit orders at the level the
                           moment the level exists (ORB High/Low, 4H
                           range edges, 8AM range edges, consolidation
                           boundary). These are valid tradeable setups
                           on their own - classic breakout orders.
  Stage 2 (trigger):       market/limit entries on the trigger event
                           (close beyond, sweep, reclaim) even when a
                           later confirmation (1m CHoCH, key-level tap,
                           momentum candle, OB) is still missing - the
                           missing confluence is stated in the note so
                           you can size down.
  Stage 3 (confirmed):     the full original chain, as before.

Each stage gets its own Alert key, so multiple setups per symbol per
session flow through dedup instead of colliding.

Everything extra is gated by config.SETUP_MODE:
    SETUP_MODE = "aggressive"  -> all three stages (default here)
    SETUP_MODE = "balanced"    -> stage 2 + stage 3 only
    SETUP_MODE = "strict"      -> stage 3 only (the original behaviour)

Bar-close logic is unchanged: wicks count as sweeps, closes count as
breaks, no repainting.

Add to config.py (defaults used if missing):
    SETUP_MODE                    = "aggressive"
    ORB_TP_R_MULTIPLE             = 2.0
    FOURH_FB_TP_MODE              = "opposite"   # "opposite" or "2R"
    RANGE_SWEEP_EDGE_ORDERS       = True         # stage-1 limits at 8AM range
    SMT_SWING_WIDTH               = 1
    SMT_MAX_AGE_HOURS             = 12
    MOMENTUM_BODY_PCT             = 0.50
    MOMENTUM_TP1_R                = 1.5
    CHANDELIER_ATR_PERIOD         = 14
    CHANDELIER_ATR_MULT           = 2.0
    TOUCH_TOLERANCE_PCT           = 0.0003
    KEYLEVEL_EQUAL_LO_TOLERANCE_PCT = 0.0004
    SWEEP_RECLAIM_LOOKBACK_SWINGS = 4
"""

from datetime import timedelta

import config
from src.candles import (
    Candle, Direction, find_swings, last_swing_before, find_fvgs, mark_mitigated,
    unmitigated_fvg_in_range, find_order_block, detect_ifvg, structure_trend, utc_now,
)
from src.killzones import ASIAN, LONDON, NEW_YORK_AM, NY_TZ
from src.strategies import Alert, _close_beyond, ALL_SYMBOLS


def _cfg(name: str, default):
    return getattr(config, name, default)


SETUP_MODE = _cfg("SETUP_MODE", "aggressive")
STAGE1 = SETUP_MODE in ("aggressive",)
STAGE2 = SETUP_MODE in ("aggressive", "balanced")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _atr(candles: list[Candle], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    ranges = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        ranges.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    return sum(ranges[-period:]) / period


def _fvg_overlapping_level(fvgs, level: float, direction: Direction, tol: float):
    out = []
    for g in fvgs:
        if g.direction != direction or g.mitigated:
            continue
        if g.bottom - tol <= level <= g.top + tol:
            out.append(g)
    return out


def _one_min_reversal(c1: list[Candle], from_index: int, direction: Direction) -> int | None:
    """1m CHoCH close beyond the most recent opposite swing, an IFVG, or a
    confirmed OB. None if none has happened yet."""
    window = c1[from_index:]
    if len(window) < 3:
        return None
    swings = find_swings(window, width=1)
    is_high = direction == Direction.BEARISH
    ref = last_swing_before(swings, len(window) - 1, is_high=is_high)
    if ref is not None:
        idx = _close_beyond(window, ref.price, ref.index + 1, direction)
        if idx is not None:
            return from_index + idx
    fvgs = find_fvgs(window)
    mark_mitigated(fvgs, window)
    ifvg = detect_ifvg(fvgs, window, from_index=2)
    if ifvg is not None:
        return from_index + min(ifvg.formed_at_index + 1, len(window) - 1)
    ob = find_order_block(window, len(window) - 1, direction)
    if ob is not None and ob.confirmed:
        return from_index + min(ob.index + 1, len(window) - 1)
    return None


def _tz(candles: list[Candle], h1: int, h2: int) -> list[Candle]:
    """Candles whose NY-time hour falls in [h1, h2). h2=0 means 'from h1 to midnight'."""
    out = []
    for c in candles:
        t = c.time.astimezone(NY_TZ)
        if h1 <= t.hour < h2 or (h2 == 0 and t.hour >= h1) or (h2 < h1 and (t.hour >= h1 or t.hour < h2)):
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# 1. ORB Breakout + Retest + SMC FVG  (high-frequency version)
#    Stage 1: buy-stop at ORB High / sell-stop at ORB Low right after the
#             9:45 candle closes.
#    Stage 2: every new unmitigated 5m FVG overlapping ORB level after a
#             5m close beyond -> pending limit (1m confirmation NOT
#             required; noted when missing).
#    Stage 3: retest/zone + 1m CHoCH/IFVG/OB confirmed.
#    Plus the false-breakout variant on ANY close-back-inside candle.
# ---------------------------------------------------------------------------

def orb_breakout_strategy(data: dict) -> list[Alert]:
    alerts: list[Alert] = []
    for symbol in ALL_SYMBOLS:
        alerts += _orb_for_symbol(symbol, data)
    return alerts


def _orb_for_symbol(symbol: str, data: dict) -> list[Alert]:
    c15 = data.get((symbol, "15M"), [])
    c5 = data.get((symbol, "5M"), [])
    c1 = data.get((symbol, "1M"), [])
    if not c15 or not c5:
        return []

    orb_candle = next(
        (c for c in c15
         if (t := c.time.astimezone(NY_TZ)).hour == 9 and t.minute == 30),
        None,
    )
    if orb_candle is None:
        return []
    orb_high, orb_low = orb_candle.high, orb_candle.low
    post = [c for c in c5 if c.time >= orb_candle.time + timedelta(minutes=15)]
    if not post:
        return []

    tp_r = _cfg("ORB_TP_R_MULTIPLE", 2.0)
    tol = orb_high * 1e-5
    alerts: list[Alert] = []

    def _risk_levels(direction, anchor_low, anchor_high):
        risk = orb_high - orb_low
        if direction == Direction.BULLISH:
            sl, tp = anchor_low, anchor_high + 0  # placeholder, replaced per alert
        return risk

    # ---- Stage 1: classic ORB stop orders (valid setups on their own) ----
    if STAGE1:
        risk = orb_high - orb_low
        if risk > 0:
            for side, entry, sl, direction in (
                ("buy", orb_high, orb_low, Direction.BULLISH),
                ("sell", orb_low, orb_high, Direction.BEARISH),
            ):
                tp = entry + tp_r * risk if direction == Direction.BULLISH else entry - tp_r * risk
                alerts.append(Alert(
                    key=f"orb_s1|{symbol}|{side}|{orb_candle.time.isoformat()}",
                    strategy="ORB stop entry",
                    symbol=symbol, direction=side,
                    entry_type="buy-stop" if side == "buy" else "sell-stop",
                    entry=entry, sl=sl, tp=tp,
                    note=f"Classic ORB: stop order at {'ORB High' if side == 'buy' else 'ORB Low'} "
                         f"({orb_low:.5g}-{orb_high:.5g}), opposite edge as SL, TP {tp_r}R. "
                         "Valid on its own - no retest/FVG needed.",
                ))

    # ---- Stage 2/3: breakout -> retest zone -> (1m confirmation) ----
    for direction, orb_level, opposite in (
        (Direction.BULLISH, orb_high, orb_low),
        (Direction.BEARISH, orb_low, orb_high),
    ):
        break_idx = _close_beyond(post, orb_level, 1, direction)
        if break_idx is None:
            continue
        fvgs = find_fvgs(post, start_index=break_idx)
        mark_mitigated(fvgs, post)
        zones = _fvg_overlapping_level(fvgs, orb_level, direction, tol)
        side = "buy" if direction == Direction.BULLISH else "sell"

        # every unmitigated overlapping FVG is its own setup (no per-run cap)
        for zone in zones:
            entry = zone.midpoint
            sl = zone.bottom if direction == Direction.BULLISH else zone.top
            risk = abs(entry - sl)
            if risk <= 0:
                continue
            tp = entry + tp_r * risk if direction == Direction.BULLISH else entry - tp_r * risk
            # Stage 3 check: 1m reversal after the break
            confirmed = False
            if c1:
                c1_from = [i for i, c in enumerate(c1) if c.time >= post[break_idx].time]
                if c1_from:
                    confirmed = _one_min_reversal(c1, c1_from[0], direction) is not None
            if confirmed or STAGE2:
                alerts.append(Alert(
                    key=f"orb_s{'3' if confirmed else '2'}|{symbol}|{side}|"
                        f"{post[zone.formed_at_index].time.isoformat()}",
                    strategy="ORB retest FVG" + ("" if confirmed else " (unconfirmed)"),
                    symbol=symbol, direction=side,
                    entry_type="limit",
                    entry=entry, sl=sl, tp=tp,
                    note=("Full ORB chain: close beyond ORB, retest FVG, 1m reversal confirmed. "
                          if confirmed else
                          "5m close beyond ORB + unmitiated retest FVG; 1m CHoCH/IFVG/OB NOT yet "
                          "confirmed - size down or wait for it. "),
                ))

        # plain retest of the level (no FVG needed) as a market-on-touch setup
        if STAGE2:
            retest_idx = next(
                (i for i in range(break_idx + 1, len(post))
                 if post[i].low <= orb_level <= post[i].high),
                None,
            )
            if retest_idx is not None:
                entry = orb_level
                sl = min(c.low for c in post[break_idx:retest_idx + 1]) \
                    if direction == Direction.BULLISH else \
                    max(c.high for c in post[break_idx:retest_idx + 1])
                risk = abs(entry - sl)
                if risk > 0:
                    tp = entry + tp_r * risk if direction == Direction.BULLISH else entry - tp_r * risk
                    alerts.append(Alert(
                        key=f"orb_s2r|{symbol}|{side}|{post[retest_idx].time.isoformat()}",
                        strategy="ORB level retest",
                        symbol=symbol, direction=side,
                        entry_type="limit",
                        entry=entry, sl=sl, tp=tp,
                        note="5m close beyond ORB + price back at the level. "
                             "No FVG/1m confirmation - aggressive retest entry.",
                    ))

    # ---- False breakout variant: any wick out + close back inside ----
    for swept_is_high in (True, False):
        level = orb_high if swept_is_high else orb_low
        direction = Direction.BEARISH if swept_is_high else Direction.BULLISH
        side = "sell" if swept_is_high else "buy"
        for fake_idx, c in enumerate(post):
            closed_back = (c.high > level and c.close < level) if swept_is_high \
                else (c.low < level and c.close > level)
            if not closed_back:
                continue
            fvgs = find_fvgs(post, start_index=max(fake_idx - 2, 0))
            mark_mitigated(fvgs, post)
            zones = _fvg_overlapping_level(fvgs, level, direction, tol)
            sl = c.high if swept_is_high else c.low
            if zones:
                zone = zones[-1]
                entry, entry_type = zone.midpoint, "limit"
                sl = max(sl, zone.top) if swept_is_high else min(sl, zone.bottom)
            elif STAGE2:
                entry, entry_type = c.close, "market"
            else:
                continue
            risk = abs(entry - sl)
            if risk <= 0:
                continue
            tp = entry - tp_r * risk if swept_is_high else entry + tp_r * risk
            confirmed = False
            if c1:
                c1_from = [i for i, x in enumerate(c1) if x.time >= c.time]
                if c1_from:
                    confirmed = _one_min_reversal(c1, c1_from[0], direction) is not None
            if not confirmed and not STAGE2:
                continue
            alerts.append(Alert(
                key=f"orb_fb{'3' if confirmed else '2'}|{symbol}|{side}|{c.time.isoformat()}",
                strategy="ORB false breakout" + ("" if confirmed else " (unconfirmed)"),
                symbol=symbol, direction=side,
                entry_type=entry_type,
                entry=entry, sl=sl, tp=tp,
                note=f"False break of ORB {'High' if swept_is_high else 'Low'}, close back inside. "
                     + ("1m reversal confirmed." if confirmed
                        else "No 1m confirmation yet - aggressive entry, size down."),
            ))
    return alerts


# ---------------------------------------------------------------------------
# 2. 4H Range False Breakout / CRT Sweep  (high-frequency version)
#    Stage 1: buy-stop at range High / sell-stop at range Low once the first
#             4H candle of the day closes.
#    Stage 2: same-candle reclaim (wick beyond + 5m close back inside) ->
#             market entry, no need to wait for the NEXT candle.
#    Stage 3: the original next-candle-reclaim model.
# ---------------------------------------------------------------------------

def fourh_range_false_breakout_strategy(data: dict) -> list[Alert]:
    alerts: list[Alert] = []
    for symbol in ALL_SYMBOLS:
        alerts += _fourh_fb_for_symbol(symbol, data)
    return alerts


def _fourh_fb_for_symbol(symbol: str, data: dict) -> list[Alert]:
    c4h = data.get((symbol, "4H"), [])
    c5 = data.get((symbol, "5M"), [])
    if not c4h or not c5:
        return []

    first_4h = None
    for c in reversed(c4h):
        if c.time.astimezone(NY_TZ).hour in (0, 4, 8, 12, 16, 20):
            first_4h = c
            break
    if first_4h is None:
        return []
    range_high, range_low = first_4h.high, first_4h.low
    post = [c for c in c5 if c.time >= first_4h.time + timedelta(hours=4)]
    if not post:
        return []

    tp_mode = _cfg("FOURH_FB_TP_MODE", "opposite")
    alerts: list[Alert] = []

    # ---- Stage 1: breakout stop orders at both range edges ----
    if STAGE1:
        risk = range_high - range_low
        if risk > 0:
            for side, entry, sl, direction in (
                ("buy", range_high, range_low, Direction.BULLISH),
                ("sell", range_low, range_high, Direction.BEARISH),
            ):
                tp = entry + 2 * risk if direction == Direction.BULLISH else entry - 2 * risk
                alerts.append(Alert(
                    key=f"4h_s1|{symbol}|{side}|{first_4h.time.isoformat()}",
                    strategy="4H range breakout",
                    symbol=symbol, direction=side,
                    entry_type="buy-stop" if side == "buy" else "sell-stop",
                    entry=entry, sl=sl, tp=tp,
                    note=f"4H range {range_low:.5g}-{range_high:.5g} stop order. "
                         "Trend-day catcher - if it goes false-break, the FB module fires the other way.",
                ))

    # ---- Stage 2/3: sweep + reclaim (every occurrence, no skip) ----
    for direction, beyond, back_inside, swept_extreme, opposite_side, swept_low in (
        (Direction.BULLISH, lambda c: c.close < range_low, lambda c: c.close > range_low,
         lambda a, b: min(a.low, b.low), range_high, True),
        (Direction.BEARISH, lambda c: c.close > range_high, lambda c: c.close < range_high,
         lambda a, b: max(a.high, b.high), range_low, False),
    ):
        side = "buy" if direction == Direction.BULLISH else "sell"
        for i in range(len(post)):
            c = post[i]
            wick_beyond = (c.low < range_low) if swept_low else (c.high > range_high)
            close_beyond = beyond(c)
            close_back = (c.close > range_low) if swept_low else (c.close < range_high)
            if not wick_beyond:
                continue
            if close_back and close_beyond and not STAGE2:
                pass  # same-candle reclaim only in aggressive mode
            elif close_back:
                # same-candle reclaim (stage 2)
                entry, sl_src = c.close, c
                stage = "2"
            elif i + 1 < len(post) and back_inside(post[i + 1]):
                # next-candle reclaim (stage 3, original model)
                entry, sl_src = post[i + 1].close, post[i]
                stage = "3"
            else:
                continue
            sl = sl_src.low if swept_low else sl_src.high
            risk = abs(entry - sl)
            if risk <= 0:
                continue
            tp = opposite_side if tp_mode == "opposite" else (
                entry + 2 * risk if direction == Direction.BULLISH else entry - 2 * risk)
            alerts.append(Alert(
                key=f"4h_fb_s{stage}|{symbol}|{side}|{c.time.isoformat()}",
                strategy="4H range false breakout",
                symbol=symbol, direction=side,
                entry_type="market",
                entry=entry, sl=sl, tp=tp,
                note=f"4H range {'low' if swept_low else 'high'} swept and reclaimed "
                     f"({'same candle' if stage == '2' else 'next candle'}). "
                     f"TP = {'opposite side (CRT)' if tp_mode == 'opposite' else '2R'}.",
            ))
    return alerts


# ---------------------------------------------------------------------------
# 3. 8AM 1H Range Sweep + IFVG + OB  (high-frequency version)
#    Stage 1: pending limits at BOTH range edges right after the 8AM candle
#             closes (you're buying the discount side / selling the premium
#             side of the range; sweep expected per the model).
#    Stage 2: sweep + IFVG formed (key-level tap NOT required - noted).
#    Stage 3: sweep at key level + IFVG + confirmed OB (original).
# ---------------------------------------------------------------------------

def _hourly_key_level_tapped(c1h: list[Candle], sweep_time, swept_low: bool, sweep_price: float) -> bool:
    prior = [c for c in c1h if c.time < sweep_time]
    if len(prior) < 3:
        return True
    tol = sweep_price * _cfg("KEYLEVEL_EQUAL_LO_TOLERANCE_PCT", 4e-4)
    swings = find_swings(prior, width=1)
    pts = [s.price for s in swings if s.is_high == (not swept_low)]
    for p in pts:
        if abs(p - sweep_price) <= tol:
            return True
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            if abs(pts[i] - pts[j]) <= tol:
                return True
    fvgs = find_fvgs(prior)
    zone_dir = Direction.BULLISH if swept_low else Direction.BEARISH
    return any(g.direction == zone_dir and g.bottom <= sweep_price <= g.top for g in fvgs)


def range_sweep_ifvg_ob_strategy(data: dict) -> list[Alert]:
    alerts: list[Alert] = []
    for symbol in ALL_SYMBOLS:
        alerts += _range_sweep_for_symbol(symbol, data)
    return alerts


def _range_sweep_for_symbol(symbol: str, data: dict) -> list[Alert]:
    c1h = data.get((symbol, "1H"), [])
    c1 = data.get((symbol, "1M"), [])
    if not c1h or not c1:
        return []

    range_candle = next((c for c in c1h if c.time.astimezone(NY_TZ).hour == 8), None)
    if range_candle is None:
        return []
    range_high, range_low = range_candle.high, range_candle.low
    later = [c for c in c1h if c.time > range_candle.time]
    if not later:
        return []
    working = [c for c in c1 if c.time >= later[0].time]
    if len(working) < 3:
        return []

    alerts: list[Alert] = []

    # ---- Stage 1: limit orders at both range edges ----
    if STAGE1 and _cfg("RANGE_SWEEP_EDGE_ORDERS", True):
        risk = range_high - range_low
        if risk > 0:
            for side, entry, sl, direction in (
                ("buy", range_low, range_low - 0.5 * risk, Direction.BULLISH),
                ("sell", range_high, range_high + 0.5 * risk, Direction.BEARISH),
            ):
                tp = range_high if direction == Direction.BULLISH else range_low
                alerts.append(Alert(
                    key=f"rs_s1|{symbol}|{side}|{range_candle.time.isoformat()}",
                    strategy="Range edge limit",
                    symbol=symbol, direction=side,
                    entry_type="limit",
                    entry=entry, sl=sl, tp=tp,
                    note=f"Limit at 8AM range {'low' if side == 'buy' else 'high'} "
                         f"({range_low:.5g}-{range_high:.5g}). Model says one side gets swept "
                         "then price targets the opposite side - this is the discount/premium entry.",
                ))

    # ---- Stage 2/3: sweep -> IFVG -> (OB) ----
    for swept_low, level, direction, tp in (
        (True, range_low, Direction.BULLISH, range_high),
        (False, range_high, Direction.BEARISH, range_low),
    ):
        side = "buy" if direction == Direction.BULLISH else "sell"
        sweep_idx = next(
            (i for i, c in enumerate(working) if (c.low < level if swept_low else c.high > level)),
            None,
        )
        if sweep_idx is None:
            continue
        sweep_price = working[sweep_idx].low if swept_low else working[sweep_idx].high
        key_tapped = _hourly_key_level_tapped(c1h, working[sweep_idx].time, swept_low, sweep_price)
        fvgs = find_fvgs(working, start_index=max(sweep_idx - 2, 0))
        mark_mitigated(fvgs, working)
        ifvg = detect_ifvg(fvgs, working, from_index=sweep_idx + 1)
        if ifvg is None:
            continue
        ob = find_order_block(working, ifvg.formed_at_index, direction)
        ob_ok = ob is not None and ob.confirmed
        stage = "3" if (key_tapped and ob_ok) else ("2" if STAGE2 else None)
        if stage is None:
            continue
        entry = min(getattr(ob, "midpoint", getattr(ob, "price", sweep_price)) if ob else sweep_price,
                    working[ifvg.formed_at_index].close)
        alerts.append(Alert(
            key=f"rs_s{stage}|{symbol}|{side}|{working[ifvg.formed_at_index].time.isoformat()}",
            strategy="Range sweep IFVG OB" + ("" if stage == "3" else " (unconfirmed)"),
            symbol=symbol, direction=side,
            entry_type="limit",
            entry=entry, sl=sweep_price, tp=tp,
            note=f"8AM range {'low' if swept_low else 'high'} swept, 1m IFVG formed. "
                 + ("Key level tapped, OB confirmed - full setup."
                    if stage == "3" else
                    f"Missing: {'key-level tap, ' if not key_tapped else ''}"
                    f"{'OB confirmation' if not ob_ok else ''} - size down."),
        ))
    return alerts


# ---------------------------------------------------------------------------
# 4. Daily Profile Session Bias  (FILTER - unchanged)
# ---------------------------------------------------------------------------

def daily_session_bias(data: dict, symbol: str = "EURUSD") -> str:
    c5 = data.get((symbol, "5M"), [])
    if not c5:
        return "neutral"
    asia = _tz(c5, 20, 0)
    london = _tz(c5, 2, 5)
    if not asia or not london:
        return "neutral"
    asia_high, asia_low = max(c.high for c in asia), min(c.low for c in asia)

    london_swept_asia_high = any(c.high > asia_high for c in london)
    london_swept_asia_low = any(c.low < asia_low for c in london)
    distributed_down = any(c.close < asia_low for c in london)
    distributed_up = any(c.close > asia_high for c in london)

    if london_swept_asia_high and not distributed_down:
        return "bullish"
    if london_swept_asia_high and distributed_down:
        return "bearish"
    if london_swept_asia_low and not distributed_up:
        return "bearish"
    if london_swept_asia_low and distributed_up:
        return "bullish"
    london_high, london_low = max(c.high for c in london), min(c.low for c in london)
    post_london = [c for c in c5 if c.time > london[-1].time]
    if post_london and any(c.high > london_high for c in post_london):
        return "bearish"
    if post_london and any(c.low < london_low for c in post_london):
        return "bullish"
    return "neutral"


# ---------------------------------------------------------------------------
# 5. SMT Divergence  (loosened: divergence + (MSS OR FVG/OB); highs AND lows;
#    wider age window -> more valid setups)
# ---------------------------------------------------------------------------

def smt_divergence_strategy(data: dict, anchor: str = "NAS100",
                            comparison: str = "EURUSD") -> list[Alert]:
    a5 = data.get((anchor, "5M"), [])
    c5 = data.get((comparison, "5M"), [])
    if len(a5) < 10 or len(c5) < 10:
        return []

    w = _cfg("SMT_SWING_WIDTH", 1)
    max_age = _cfg("SMT_MAX_AGE_HOURS", 12) * 3600
    sa = find_swings(a5, width=w)
    sc = find_swings(c5, width=w)
    a_lows = [s for s in sa if not s.is_high][-2:]
    c_lows = [s for s in sc if not s.is_high][-2:]
    a_highs = [s for s in sa if s.is_high][-2:]
    c_highs = [s for s in sc if s.is_high][-2:]

    direction = None
    if len(a_lows) == 2 and len(c_lows) == 2 and \
            a_lows[-1].price < a_lows[-2].price and c_lows[-1].price > c_lows[-2].price:
        direction = Direction.BULLISH
        pivot_time = a_lows[-1].time
    elif len(a_highs) == 2 and len(c_highs) == 2 and \
            a_highs[-1].price > a_highs[-2].price and c_highs[-1].price < c_highs[-2].price:
        direction = Direction.BEARISH
        pivot_time = a_highs[-1].time
    if direction is None:
        return []
    if (utc_now() - pivot_time).total_seconds() > max_age:
        return []

    swings = find_swings(a5, width=1)
    ref = last_swing_before(swings, len(a5) - 1, is_high=(direction == Direction.BEARISH))
    if ref is None:
        return []
    mss_idx = _close_beyond(a5, ref.price, ref.index + 1, direction)
    fvgs = find_fvgs(a5, start_index=max((mss_idx or ref.index) - 2, 0))
    mark_mitigated(fvgs, a5)
    gap = unmitigated_fvg_in_range(fvgs, a5, mss_idx or ref.index, len(a5) - 1, direction) \
        if mss_idx is not None else None
    ob = find_order_block(a5, mss_idx or ref.index, direction)
    ob_ok = ob is not None and ob.confirmed
    # loosened: need MSS + at least one displacement signature (FVG or OB)
    if mss_idx is None or (gap is None and not ob_ok):
        return []

    entry = gap.midpoint if gap is not None else ob.price
    sl = ref.price
    risk = abs(entry - sl)
    if risk <= 0:
        return []
    tp = entry + 2 * risk if direction == Direction.BULLISH else entry - 2 * risk
    side = "buy" if direction == Direction.BULLISH else "sell"
    return [Alert(
        key=f"smt|{anchor}|{side}|{a5[mss_idx].time.isoformat()}",
        strategy="SMT divergence",
        symbol=anchor, direction=side,
        entry_type="limit",
        entry=entry, sl=sl, tp=tp,
        note=f"SMT vs {comparison} + MSS. {'FVG entry.' if gap else 'OB entry.'} "
             "Confirmation module - check HTF bias before taking it.",
    )]


# ---------------------------------------------------------------------------
# 6. Liquidity Pool Targets  (helper - unchanged)
# ---------------------------------------------------------------------------

def liquidity_pool_targets(symbol: str, data: dict, direction: Direction) -> list[tuple[float, str]]:
    pools: list[tuple[float, str]] = []
    c5 = data.get((symbol, "5M"), [])
    c15 = data.get((symbol, "15M"), [])
    for label, candles in (("5M", c5), ("15M", c15)):
        if not candles:
            continue
        fvgs = find_fvgs(candles)
        mark_mitigated(fvgs, candles)
        for g in fvgs:
            if g.mitigated:
                continue
            edge = g.top if direction == Direction.BULLISH else g.bottom
            pools.append((edge, f"unfilled {label} FVG (priority target)"))
    if c15:
        swings = find_swings(c15, width=1)
        pts = [s.price for s in swings if s.is_high == (direction == Direction.BEARISH)]
        for p in pts[-3:]:
            pools.append((p, "15M intermediate high/low"))
        tol = c15[-1].close * 1e-4
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                if abs(pts[i] - pts[j]) <= tol:
                    pools.append((pts[i], "relative equal lows/highs (liquidity)"))
    if c5:
        for name, (h1, h2) in (("Asia", (20, 0)), ("London", (2, 5))):
            sess = _tz(c5, h1, h2)
            if sess:
                lvl = min(c.low for c in sess) if direction == Direction.BULLISH else max(c.high for c in sess)
                pools.append((lvl, f"{name} session {'low' if direction == Direction.BULLISH else 'high'}"))
    seen, ranked = set(), []
    for price, label in pools:
        key = round(price, 6)
        if key not in seen:
            seen.add(key)
            ranked.append((price, label))
    return ranked


# ---------------------------------------------------------------------------
# 7. Breakout + Momentum  (high-frequency version)
#    Stage 1: stop orders at the consolidation boundary once >=2 touches
#             exist.
#    Stage 2: ANY 5m close beyond the level alerts, momentum candle or not
#             (noted "no momentum - half size"); 2 consecutive candles count
#             as momentum (was 3).
#    Stage 3: original momentum-candle requirement.
# ---------------------------------------------------------------------------

def breakout_momentum_strategy(data: dict) -> list[Alert]:
    alerts: list[Alert] = []
    for symbol in ALL_SYMBOLS:
        alerts += _breakout_momentum_for_symbol(symbol, data)
    return alerts


def _breakout_momentum_for_symbol(symbol: str, data: dict) -> list[Alert]:
    c5 = data.get((symbol, "5M"), [])
    if len(c5) < 20:
        return []
    body_pct_min = _cfg("MOMENTUM_BODY_PCT", 0.50)
    atr = _atr(c5, _cfg("CHANDELIER_ATR_PERIOD", 14))
    if atr is None:
        return []

    swings = find_swings(c5, width=2)
    highs = [s for s in swings if s.is_high]
    lows = [s for s in swings if not s.is_high]
    tol = c5[-1].close * _cfg("TOUCH_TOLERANCE_PCT", 3e-4)
    alerts: list[Alert] = []

    for direction, level_swings, is_high in (
        (Direction.BULLISH, highs, True),
        (Direction.BEARISH, lows, False),
    ):
        if len(level_swings) < 2:
            continue
        level = None
        cluster = []
        for i in range(len(level_swings) - 1):
            for j in range(i + 1, len(level_swings)):
                if abs(level_swings[i].price - level_swings[j].price) <= tol:
                    level = max(level_swings[i].price, level_swings[j].price) if is_high \
                        else min(level_swings[i].price, level_swings[j].price)
                    cluster = [level_swings[i], level_swings[j]]
                    break
            if level is not None:
                break
        if level is None:
            continue
        side = "buy" if direction == Direction.BULLISH else "sell"
        beyond = (lambda c: c.close > level) if is_high else (lambda c: c.close < level)

        # ---- Stage 1: stop order at the boundary ----
        if STAGE1:
            swing_lows = [s.price for s in lows[-3:]] or [min(c.low for c in c5[-30:])]
            swing_highs = [s.price for s in highs[-3:]] or [max(c.high for c in c5[-30:])]
            sl = min(swing_lows) if direction == Direction.BULLISH else max(swing_highs)
            risk = abs(level - sl)
            if risk > 0:
                tp = level + 2 * risk if direction == Direction.BULLISH else level - 2 * risk
                alerts.append(Alert(
                    key=f"bm_s1|{symbol}|{side}|{cluster[-1].time.isoformat()}",
                    strategy="Consolidation breakout stop",
                    symbol=symbol, direction=side,
                    entry_type="buy-stop" if side == "buy" else "sell-stop",
                    entry=level, sl=sl, tp=tp,
                    note=f"Consolidation at {level:.5g} with >=2 touches - stop order at the boundary. "
                         "Chandelier-trail the runner if it goes (ATR14 x2).",
                ))

        # ---- Stage 2/3: close beyond the level ----
        for i, c in enumerate(c5):
            if not beyond(c):
                continue
            big_body = c.body_pct_of_range >= body_pct_min and (
                c.is_bullish if direction == Direction.BULLISH else c.is_bearish)
            two_in_row = i >= 1 and all(
                (c5[k].is_bullish if direction == Direction.BULLISH else c5[k].is_bearish)
                for k in (i - 1, i))
            three_in_row = i >= 2 and all(
                (c5[k].is_bullish if direction == Direction.BULLISH else c5[k].is_bearish)
                for k in (i - 2, i - 1, i))
            momentum = big_body or three_in_row
            if not momentum and not (STAGE2 and two_in_row):
                continue
            stage = "3" if momentum else "2"
            entry = c.close
            swing_lows = [s.price for s in lows[-3:]] or [min(x.low for x in c5[max(0, i - 30):i + 1])]
            swing_highs = [s.price for s in highs[-3:]] or [max(x.high for x in c5[max(0, i - 30):i + 1])]
            sl = min(swing_lows) if direction == Direction.BULLISH else max(swing_highs)
            if (direction == Direction.BULLISH and sl >= entry) or (direction == Direction.BEARISH and sl <= entry):
                sl = level
            risk = abs(entry - sl)
            if risk <= 0:
                continue
            tp1 = entry + _cfg("MOMENTUM_TP1_R", 1.5) * risk if direction == Direction.BULLISH \
                else entry - _cfg("MOMENTUM_TP1_R", 1.5) * risk
            chandelier = (c.high - _cfg("CHANDELIER_ATR_MULT", 2.0) * atr) if direction == Direction.BULLISH \
                else (c.low + _cfg("CHANDELIER_ATR_MULT", 2.0) * atr)
            alerts.append(Alert(
                key=f"bm_s{stage}|{symbol}|{side}|{c.time.isoformat()}",
                strategy="Breakout momentum" + ("" if stage == "3" else " (no momentum)"),
                symbol=symbol, direction=side,
                entry_type="market",
                entry=entry, sl=sl, tp=tp1,
                note=(f"Consolidation breakout at {level:.5g} with momentum. "
                      if stage == "3" else
                      f"Close beyond {level:.5g} WITHOUT a momentum candle - half size. ")
                     + f"TP1 = 1.5R (sell half, move SL to {tp1:.5g}); trail remainder with "
                       f"Chandelier stop ~{chandelier:.5g} (ATR14 x2). Exit on colour flip.",
            ))
    return alerts


# ---------------------------------------------------------------------------
# 7b. Sweep & Reclaim scalp  (NEW - the biggest setup generator)
#    Any 15m swing high/low (or session high/low) that gets swept by a wick
#    and then reclaimed by a 5m close back inside -> market entry, SL beyond
#    the sweep wick, TP 2R or the next liquidity pool. Fires all day across
#    all three symbols independent of killzones.
# ---------------------------------------------------------------------------

def sweep_reclaim_strategy(data: dict) -> list[Alert]:
    alerts: list[Alert] = []
    for symbol in ALL_SYMBOLS:
        alerts += _sweep_reclaim_for_symbol(symbol, data)
    return alerts


def _sweep_reclaim_for_symbol(symbol: str, data: dict) -> list[Alert]:
    c15 = data.get((symbol, "15M"), [])
    c5 = data.get((symbol, "5M"), [])
    if len(c15) < 10 or len(c5) < 10:
        return []

    n = _cfg("SWEEP_RECLAIM_LOOKBACK_SWINGS", 4)
    swings = find_swings(c15, width=1)[-n:]
    levels = [(s.price, True) for s in swings if s.is_high] + \
             [(s.price, False) for s in swings if not s.is_high]
    for h1, h2 in ((20, 0), (2, 5), (8, 12)):   # Asia / London / NY morning highs-lows
        sess = _tz(c5, h1, h2)
        if len(sess) >= 3:
            levels.append((max(c.high for c in sess), True))
            levels.append((min(c.low for c in sess), False))

    out: list[Alert] = []
    for level, is_high in levels:
        direction = Direction.BEARISH if is_high else Direction.BULLISH
        side = "sell" if is_high else "buy"
        for i, c in enumerate(c5):
            swept = (c.high > level) if is_high else (c.low < level)
            if not swept:
                continue
            closed_back = (c.close < level) if is_high else (c.close > level)
            if not closed_back:
                continue
            entry = c.close
            sl = c.high if is_high else c.low
            risk = abs(entry - sl)
            if risk <= 0:
                continue
            tp = entry - 2 * risk if is_high else entry + 2 * risk
            pools = liquidity_pool_targets(symbol, data, direction)
            pool_note = f" Next liquidity: {pools[0][0]:.5g} ({pools[0][1]})." if pools else ""
            out.append(Alert(
                key=f"sr|{symbol}|{side}|{level:.6g}|{c.time.isoformat()}",
                strategy="Sweep & reclaim",
                symbol=symbol, direction=side,
                entry_type="market",
                entry=entry, sl=sl, tp=tp,
                note=f"15m/session {'high' if is_high else 'low'} {level:.5g} swept and reclaimed "
                     f"on the 5m close. SL beyond the sweep wick, TP 2R.{pool_note}",
            ))
            break   # one reclaim per level
    return out


# ---------------------------------------------------------------------------
# 8. Top-Down MTF Framework  (FILTER - unchanged)
# ---------------------------------------------------------------------------

def topdown_bias(data: dict, symbol: str) -> Direction | None:
    c1h = data.get((symbol, "1H"), [])
    c15 = data.get((symbol, "15M"), [])
    c5 = data.get((symbol, "5M"), [])
    if not c1h or not c15 or not c5:
        return None
    htf = structure_trend(c1h, width=1)
    if htf is None:
        return None
    mtf = structure_trend(c15, width=1)
    if mtf != htf:
        return None
    ltf = structure_trend(c5, width=1)
    return htf if ltf in (None, htf) else None


def filter_with_topdown(alerts: list[Alert], data: dict) -> list[Alert]:
    """HTF filter per the spec. In aggressive mode it only DOWNSIZES
    counter-trend alerts (keeps them, appends a warning) instead of
    dropping them, so setup count is preserved."""
    kept = []
    for a in alerts:
        bias = topdown_bias(data, a.symbol)
        agrees = bias is not None and (
            (bias == Direction.BULLISH and a.direction == "buy") or
            (bias == Direction.BEARISH and a.direction == "sell"))
        if agrees or bias is None or STAGE2:
            if bias is not None and not agrees:
                a.note += " ⚠️ Counter-trend vs 1H/15M structure - half size."
            kept.append(a)
    return kept


ALL_SMC_STRATEGIES = [
    orb_breakout_strategy,
    fourh_range_false_breakout_strategy,
    range_sweep_ifvg_ob_strategy,
    smt_divergence_strategy,
    breakout_momentum_strategy,
    sweep_reclaim_strategy,
]
