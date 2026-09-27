"""
Fourteen mechanical strategies. Strategies 1-5 are the original set (renamed
with a bracketed number, logic unchanged from the proactive rewrite).
Strategies 6-10 are from the "ChartTactix" specification.
Strategies 11-14 are additional entry models:

  11. ORB Breakout + Retest + FVG
  12. 4H Range False Breakout / CRT
  13. Breakout + Momentum + Chandelier Exit (trail instructions in note only)
  14. 8AM 1H Range Sweep + IFVG + OB

Strategies 8 and 9 need a correlated instrument's candles in `data`
(CORRELATED_PAIR below) - if that data isn't present, they return no
alert rather than fabricate an SMT signal, per project rule.
"""

from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

import config
from src.candles import (
    Candle, Direction, find_swings, last_swing_before, find_fvgs, mark_mitigated,
    unmitigated_fvg_in_range, find_order_block, find_all_order_blocks, detect_ifvg,
    find_bpr_zones, find_breaker_blocks, detect_smt_divergence, structure_trend, utc_now,
    atr,
)
from src.killzones import ASIAN, LONDON, NEW_YORK_AM, NY_TZ, pre_london_window



@dataclass
class Alert:
    key: str
    strategy: str
    symbol: str
    direction: str
    entry_type: str
    entry: float
    sl: float
    tp: float
    note: str = ""


def _close_beyond(candles, level, from_index, direction):
    for i in range(from_index, len(candles)):
        c = candles[i]
        if direction == Direction.BULLISH and c.close > level:
            return i
        if direction == Direction.BEARISH and c.close < level:
            return i
    return None


def _zone_tap(candles, top, bottom, from_index):
    for i in range(from_index, len(candles)):
        c = candles[i]
        if c.low <= top and c.high >= bottom:
            return i
    return None


def _reaction_candle(candles, from_index, direction):
    for i in range(from_index, len(candles)):
        c = candles[i]
        if direction == Direction.BULLISH and c.is_bullish:
            return i
        if direction == Direction.BEARISH and c.is_bearish:
            return i
    return None


ALL_SYMBOLS = ("NAS100", "XAUUSD", "EURUSD")

# Correlated instrument used for SMT (Smart Money Divergence) checks -
# strategies 8 and 9 need this symbol's data present in `data` or they
# correctly produce no alert rather than fabricate one.
CORRELATED_PAIR = {
    "NAS100": "US500",
    "XAUUSD": "XAGUSD",
    "EURUSD": "GBPUSD",
}


# ---------------------------------------------------------------------------
# 1. "Asians" (Strategy 1)
# ---------------------------------------------------------------------------

def asians_strategy(data: dict) -> list[Alert]:
    alerts: list[Alert] = []
    for symbol in ALL_SYMBOLS:
        alerts += _asians_for_symbol(symbol, data)
    return alerts


def _asians_for_symbol(symbol: str, data: dict) -> list[Alert]:
    c5 = data.get((symbol, "5M"), [])
    c15 = data.get((symbol, "15M"), [])
    c1h = data.get((symbol, "1H"), [])
    if not c5 or not c1h:
        return []

    now = utc_now()
    asian_start, asian_end = ASIAN.current_or_most_recent_window(now)
    asian_candles = [c for c in c5 if asian_start <= c.time < asian_end]
    if not asian_candles:
        return []
    asian_high = max(c.high for c in asian_candles)
    asian_low = min(c.low for c in asian_candles)

    post_asian = [c for c in c5 if c.time >= asian_end]
    if len(post_asian) < 3:
        return []

    trend = structure_trend(c1h, width=1)
    if trend is None:
        return []
    uptrend = trend == Direction.BULLISH

    alerts: list[Alert] = []
    for swept_is_high in (True, False):
        level = asian_high if swept_is_high else asian_low
        sweep_idx = next(
            (i for i, c in enumerate(post_asian) if (c.high > level if swept_is_high else c.low < level)),
            None,
        )
        if sweep_idx is None:
            continue
        if swept_is_high:
            mode, side_dir = ("continuation", Direction.BULLISH) if uptrend else ("reversal", Direction.BEARISH)
        else:
            mode, side_dir = ("continuation", Direction.BEARISH) if not uptrend else ("reversal", Direction.BULLISH)
        if mode == "continuation":
            alert = _asians_continuation(symbol, post_asian, sweep_idx, side_dir, level)
        else:
            alert = _asians_reversal(symbol, c15, post_asian, sweep_idx, side_dir, level, swept_is_high)
        if alert:
            alerts.append(alert)
    return alerts


def _asians_continuation(symbol, working, sweep_idx, direction, swept_level):
    bos_idx = None
    for i in range(sweep_idx + 1, len(working)):
        swings = find_swings(working[: i + 1], width=1)
        ref_swing = last_swing_before(swings, i, is_high=(direction == Direction.BEARISH))
        if ref_swing is None:
            continue
        c = working[i]
        if direction == Direction.BULLISH and c.close > ref_swing.price:
            bos_idx = i
            break
        if direction == Direction.BEARISH and c.close < ref_swing.price:
            bos_idx = i
            break
    if bos_idx is None:
        return None

    fvgs = find_fvgs(working, start_index=max(bos_idx - 2, 0))
    mark_mitigated(fvgs, working)
    gap = unmitigated_fvg_in_range(fvgs, working, bos_idx, len(working) - 1, direction)
    if gap is None:
        return None

    entry = gap.midpoint
    sl = gap.bottom if direction == Direction.BULLISH else gap.top
    risk = abs(entry - sl)
    tp = entry + 2 * risk if direction == Direction.BULLISH else entry - 2 * risk

    return Alert(
        key=f"asians|{symbol}|continuation|{working[gap.formed_at_index].time.isoformat()}",
        strategy="Asians (Strategy 1)",
        symbol=symbol,
        direction="buy" if direction == Direction.BULLISH else "sell",
        entry_type="limit",
        entry=entry, sl=sl, tp=tp,
        note="Pending limit order at the FVG - alerted at formation.",
    )


def _asians_reversal(symbol, c15, working, sweep_idx, direction, swept_level, swept_is_high):
    fvgs_15 = find_fvgs(c15)
    mark_mitigated(fvgs_15, c15)
    zone_direction = Direction.BEARISH if swept_is_high else Direction.BULLISH
    candidate_zones = [g for g in fvgs_15 if g.direction == zone_direction and not g.mitigated]
    if not candidate_zones:
        return None
    zone = candidate_zones[-1]

    swings = find_swings(working[: sweep_idx + 1], width=1)
    ref_swing = last_swing_before(swings, sweep_idx, is_high=not swept_is_high)
    if ref_swing is None:
        return None
    choch_idx = _close_beyond(working, ref_swing.price, sweep_idx + 1, direction)
    if choch_idx is None:
        return None

    entry = zone.midpoint
    sl = zone.bottom if direction == Direction.BULLISH else zone.top
    risk = abs(entry - sl)
    tp = entry + 2 * risk if direction == Direction.BULLISH else entry - 2 * risk

    return Alert(
        key=f"asians|{symbol}|reversal|{working[choch_idx].time.isoformat()}",
        strategy="Asians (Strategy 1)",
        symbol=symbol,
        direction="buy" if direction == Direction.BULLISH else "sell",
        entry_type="limit",
        entry=entry, sl=sl, tp=tp,
        note="Pending limit order at the 15M zone - alerted right after the CHoCH confirms.",
    )


# ---------------------------------------------------------------------------
# 2. "London tt" (Strategy 2)
# ---------------------------------------------------------------------------

def london_tt_strategy(data: dict) -> list[Alert]:
    alerts: list[Alert] = []
    for symbol in ALL_SYMBOLS:
        c15 = data.get((symbol, "15M"), [])
        c30 = data.get((symbol, "30M"), [])
        c1h = data.get((symbol, "1H"), [])
        c5 = data.get((symbol, "5M"), [])
        if not c15 or not c30 or not c5:
            continue

        now = utc_now()
        asian_start, asian_end = ASIAN.current_or_most_recent_window(now)
        asian_15 = [c for c in c15 if asian_start <= c.time < asian_end]
        if not asian_15:
            continue
        asian_trend = structure_trend(asian_15, width=1)
        if asian_trend is None:
            continue

        asian_5 = [c for c in c5 if asian_start <= c.time < asian_end]
        if not asian_5:
            continue
        asian_high = max(c.high for c in asian_5)
        asian_low = min(c.low for c in asian_5)
        asian_range = asian_high - asian_low
        if asian_range <= 0:
            continue

        _, pre_london_end = pre_london_window(now)
        pre_london_candles = [c for c in c5 if asian_end <= c.time < pre_london_end]
        if not pre_london_candles:
            continue

        if asian_trend == Direction.BULLISH:
            pullback_extreme = min(c.low for c in pre_london_candles)
            pullback_size = asian_high - pullback_extreme
        else:
            pullback_extreme = max(c.high for c in pre_london_candles)
            pullback_size = pullback_extreme - asian_low
        if pullback_size < config.LONDON_TT_MIN_PULLBACK_PCT * asian_range:
            continue

        london_15 = [c for c in c15 if c.time >= pre_london_end]
        london_30 = [c for c in c30 if c.time >= pre_london_end]
        london_1h = [c for c in c1h if c.time >= pre_london_end] if c1h else []
        if not london_15 or not london_30:
            continue

        confirm_15 = london_15[0]
        confirm_30 = london_30[0]
        confirm_1h = london_1h[0] if london_1h else None

        trend_is_bull = asian_trend == Direction.BULLISH
        c15_agrees = confirm_15.is_bullish if trend_is_bull else confirm_15.is_bearish
        c30_agrees = confirm_30.is_bullish if trend_is_bull else confirm_30.is_bearish
        c1h_agrees = None
        if confirm_1h is not None:
            c1h_agrees = confirm_1h.is_bullish if trend_is_bull else confirm_1h.is_bearish

        weak_15 = confirm_15.body_pct_of_range < config.LONDON_TT_WEAK_BODY_PCT
        valid = (c15_agrees and c30_agrees) or (weak_15 and c30_agrees and (c1h_agrees is not False))
        if not valid:
            continue

        direction = Direction.BULLISH if trend_is_bull else Direction.BEARISH
        entry = confirm_15.high if direction == Direction.BULLISH else confirm_15.low
        sl = confirm_15.low if direction == Direction.BULLISH else confirm_15.high
        risk = abs(entry - sl)
        tp = entry + risk if direction == Direction.BULLISH else entry - risk

        alerts.append(Alert(
            key=f"london_tt|{symbol}|{confirm_15.time.isoformat()}",
            strategy="London tt (Strategy 2)",
            symbol=symbol,
            direction="buy" if direction == Direction.BULLISH else "sell",
            entry_type="buy-stop" if direction == Direction.BULLISH else "sell-stop",
            entry=entry, sl=sl, tp=tp,
            note="TP shown is 1:1 minimum.",
        ))
    return alerts


# ---------------------------------------------------------------------------
# 3. "Asian tt" (Strategy 3)
# ---------------------------------------------------------------------------

def asian_tt_strategy(data: dict) -> list[Alert]:
    alerts: list[Alert] = []
    for symbol in ALL_SYMBOLS:
        alerts += _asian_tt_for_symbol(symbol, data)
    return alerts


def _asian_tt_for_symbol(symbol: str, data: dict) -> list[Alert]:
    c5 = data.get((symbol, "5M"), [])
    if not c5:
        return []

    now = utc_now()
    asian_start, asian_end = ASIAN.current_or_most_recent_window(now)
    asian_candles = [c for c in c5 if asian_start <= c.time < asian_end]
    if not asian_candles:
        return []
    asian_high = max(c.high for c in asian_candles)
    asian_low = min(c.low for c in asian_candles)
    post_asian = [c for c in c5 if c.time >= asian_end]
    if len(post_asian) < 3:
        return []

    results = []
    for swept_is_high, level in ((True, asian_high), (False, asian_low)):
        sweep_idx = next(
            (i for i, c in enumerate(post_asian) if (c.high > level if swept_is_high else c.low < level)),
            None,
        )
        if sweep_idx is None:
            continue
        direction = Direction.BEARISH if swept_is_high else Direction.BULLISH
        swings = find_swings(post_asian[: sweep_idx + 1], width=1)
        ref_swing = last_swing_before(swings, sweep_idx, is_high=not swept_is_high)
        if ref_swing is None:
            continue
        choch_idx = _close_beyond(post_asian, ref_swing.price, sweep_idx + 1, direction)
        if choch_idx is None:
            continue
        fvgs = find_fvgs(post_asian, start_index=max(sweep_idx - 2, 0))
        mark_mitigated(fvgs, post_asian)
        gap = unmitigated_fvg_in_range(fvgs, post_asian, sweep_idx, choch_idx, direction)
        if gap is None:
            continue
        results.append((choch_idx, direction, gap, ref_swing, level))

    if not results:
        return []
    results.sort(key=lambda r: r[0])
    choch_idx, direction, gap, ref_swing, opposite_extreme_level = results[0]
    entry = gap.midpoint
    sl = ref_swing.price
    tp = asian_low if direction == Direction.BEARISH else asian_high

    return [Alert(
        key=f"asian_tt|{symbol}|{post_asian[choch_idx].time.isoformat()}",
        strategy="Asian tt (Strategy 3)",
        symbol=symbol,
        direction="buy" if direction == Direction.BULLISH else "sell",
        entry_type="limit",
        entry=entry, sl=sl, tp=tp,
        note="Pending limit order, no expiry.",
    )]


# ---------------------------------------------------------------------------
# 4 & 5. "newyork" / "newyork tt" (Strategy 4 / Strategy 5)
# ---------------------------------------------------------------------------

def _newyork_cr(symbol: str, data: dict, sl_buffer: float) -> list[Alert]:
    c1h = data.get((symbol, "1H"), [])
    c1m = data.get((symbol, "1M"), [])
    if not c1h or not c1m:
        return []

    now = utc_now()
    ny_start, ny_end = NEW_YORK_AM.current_or_most_recent_window(now)
    if now >= ny_end:
        return []

    eight_am_candle = next((c for c in c1h if c.time.astimezone(NY_TZ).hour == 8), None)
    if eight_am_candle is None:
        return []
    range_high, range_low = eight_am_candle.high, eight_am_candle.low

    later_1h = [c for c in c1h if c.time > eight_am_candle.time]
    if not later_1h:
        return []
    next_hour = later_1h[0]

    if next_hour.close > range_high:
        direction = Direction.BEARISH
    elif next_hour.close < range_low:
        direction = Direction.BULLISH
    else:
        return []

    working = [c for c in c1m if c.time >= next_hour.time]
    if len(working) < 3:
        return []

    fvgs = find_fvgs(working)
    mark_mitigated(fvgs, working)
    ifvg = detect_ifvg(fvgs, working, from_index=2)
    if ifvg is None:
        return []

    ob = find_order_block(working, ifvg.formed_at_index, direction)
    if ob is None or not ob.confirmed:
        return []

    entry_candle = working[min(ifvg.formed_at_index + 1, len(working) - 1)]
    entry = entry_candle.close
    swings = find_swings(working[: ifvg.formed_at_index + 1], width=1)
    ref_swing = last_swing_before(swings, ifvg.formed_at_index, is_high=(direction == Direction.BEARISH))
    if ref_swing is None:
        return []
    sl = ref_swing.price + sl_buffer if direction == Direction.BEARISH else ref_swing.price - sl_buffer
    tp = range_low if direction == Direction.BEARISH else range_high

    strategy_name = "newyork (Strategy 4)" if symbol == "NAS100" else "newyork tt (Strategy 5)"
    return [Alert(
        key=f"{strategy_name}|{symbol}|{eight_am_candle.time.isoformat()}",
        strategy=strategy_name,
        symbol=symbol,
        direction="buy" if direction == Direction.BULLISH else "sell",
        entry_type="market",
        entry=entry, sl=sl, tp=tp,
        note=f"9AM CR model - 8AM NY range {range_low:.5g}-{range_high:.5g} taken, IFVG+OB confirmed.",
    )]


def newyork_strategy(data: dict) -> list[Alert]:
    return _newyork_cr("NAS100", data, config.NAS100_SL_BUFFER_POINTS)


def newyork_tt_strategy(data: dict) -> list[Alert]:
    alerts = []
    alerts += _newyork_cr("XAUUSD", data, config.XAUUSD_SL_BUFFER)
    alerts += _newyork_cr("EURUSD", data, config.EURUSD_SL_BUFFER_PIPS * config.EURUSD_PIP)
    return alerts


# ---------------------------------------------------------------------------
# Shared helper for the session-agnostic new strategies (6, 7, 10):
# splits the lookback window in half, uses the first half to define a
# level to sweep, searches the second half for that sweep + an MSS
# (close beyond the opposing swing). Same shape as the Asian-session
# strategies above, just without the session restriction.
# ---------------------------------------------------------------------------

def _generic_sweep_and_mss(candles):
    """Returns (working, sweep_idx, mss_idx, direction) for the most
    recent sweep+MSS found, or None if no clean setup exists right now.
    `working` is the second half of candles (index-aligned to sweep_idx/
    mss_idx); `direction` is the MSS's direction (the trade direction)."""
    if len(candles) < 40:
        return None
    split = len(candles) // 2
    reference = candles[:split]
    working = candles[split:]
    if not reference or len(working) < 5:
        return None

    ref_high = max(c.high for c in reference)
    ref_low = min(c.low for c in reference)

    best = None
    for swept_is_high, level in ((True, ref_high), (False, ref_low)):
        sweep_idx = next(
            (i for i, c in enumerate(working) if (c.high > level if swept_is_high else c.low < level)),
            None,
        )
        if sweep_idx is None:
            continue
        for direction in (Direction.BULLISH, Direction.BEARISH):
            swings = find_swings(working[: sweep_idx + 1], width=1)
            ref_swing = last_swing_before(swings, sweep_idx, is_high=(direction == Direction.BEARISH))
            if ref_swing is None:
                continue
            mss_idx = _close_beyond(working, ref_swing.price, sweep_idx + 1, direction)
            if mss_idx is None:
                continue
            candidate = (sweep_idx, mss_idx, direction)
            if best is None or mss_idx < best[1]:
                best = candidate

    if best is None:
        return None
    sweep_idx, mss_idx, direction = best
    return working, sweep_idx, mss_idx, direction


# ---------------------------------------------------------------------------
# 6. "Liquidity Sweep + MSS + FVG" (Strategy 6) - session-agnostic
# ---------------------------------------------------------------------------

def liquidity_sweep_mss_fvg_strategy(data: dict) -> list[Alert]:
    alerts = []
    for symbol in ALL_SYMBOLS:
        c15 = data.get((symbol, "15M"), [])
        if not c15:
            continue
        result = _generic_sweep_and_mss(c15)
        if result is None:
            continue
        working, sweep_idx, mss_idx, direction = result

        fvgs = find_fvgs(working, start_index=max(mss_idx - 2, 0))
        mark_mitigated(fvgs, working)
        gap = unmitigated_fvg_in_range(fvgs, working, mss_idx, len(working) - 1, direction)
        if gap is None:
            continue

        entry = gap.midpoint
        sl = gap.bottom if direction == Direction.BULLISH else gap.top
        risk = abs(entry - sl)
        tp = entry + 2 * risk if direction == Direction.BULLISH else entry - 2 * risk

        alerts.append(Alert(
            key=f"strat6|{symbol}|{working[gap.formed_at_index].time.isoformat()}",
            strategy="Liquidity Sweep + MSS + FVG (Strategy 6)",
            symbol=symbol,
            direction="buy" if direction == Direction.BULLISH else "sell",
            entry_type="limit",
            entry=entry, sl=sl, tp=tp,
            note="Session-agnostic sweep+structure-break+FVG - not tied to a specific killzone.",
        ))
    return alerts


# ---------------------------------------------------------------------------
# 7. "Liquidity Sweep + BPR" (Strategy 7) - session-agnostic
# ---------------------------------------------------------------------------

def liquidity_sweep_bpr_strategy(data: dict) -> list[Alert]:
    alerts = []
    for symbol in ALL_SYMBOLS:
        c15 = data.get((symbol, "15M"), [])
        if not c15 or len(c15) < 40:
            continue
        split = len(c15) // 2
        reference = c15[:split]
        working = c15[split:]
        if not reference or len(working) < 5:
            continue

        ref_high = max(c.high for c in reference)
        ref_low = min(c.low for c in reference)
        swept = any(c.high > ref_high for c in working) or any(c.low < ref_low for c in working)
        if not swept:
            continue

        raw_fvgs = find_fvgs(working)
        bpr_zones = find_bpr_zones(raw_fvgs)
        if not bpr_zones:
            continue
        zone = bpr_zones[-1]  # most recent BPR

        direction = zone.direction
        entry = zone.midpoint
        sl = zone.bottom if direction == Direction.BULLISH else zone.top
        risk = abs(entry - sl)
        tp = entry + 2 * risk if direction == Direction.BULLISH else entry - 2 * risk

        alerts.append(Alert(
            key=f"strat7|{symbol}|{working[zone.newer_fvg_index].time.isoformat()}",
            strategy="Liquidity Sweep + BPR (Strategy 7)",
            symbol=symbol,
            direction="buy" if direction == Direction.BULLISH else "sell",
            entry_type="limit",
            entry=entry, sl=sl, tp=tp,
            note="Entry at a Balanced Price Range (overlapping opposite-direction FVGs) after a liquidity sweep.",
        ))
    return alerts


# ---------------------------------------------------------------------------
# 8. "SMT + MSS + IFVG" (Strategy 8)
# 9. "SMT + MSS + Breaker Block" (Strategy 9)
# Both need the correlated instrument's candles present in `data` -
# if missing, correctly produce no alert (never fabricate SMT).
# ---------------------------------------------------------------------------

def smt_mss_ifvg_strategy(data: dict) -> list[Alert]:
    alerts = []
    for symbol in ALL_SYMBOLS:
        c15 = data.get((symbol, "15M"), [])
        correlated = CORRELATED_PAIR[symbol]
        corr15 = data.get((correlated, "15M"), [])
        if not c15 or not corr15:
            continue

        smt_direction = detect_smt_divergence(c15, corr15, lookback=30)
        if smt_direction is None:
            continue

        swings = find_swings(c15, width=1)
        ref_swing = last_swing_before(swings, len(c15) - 1, is_high=(smt_direction == Direction.BEARISH))
        if ref_swing is None:
            continue
        mss_idx = _close_beyond(c15, ref_swing.price, ref_swing.index + 1, smt_direction)
        if mss_idx is None:
            continue

        fvgs = find_fvgs(c15, start_index=max(mss_idx - 2, 0))
        ifvg = detect_ifvg([g for g in fvgs if g.direction == smt_direction], c15, from_index=mss_idx)
        if ifvg is None:
            continue

        entry = ifvg.midpoint
        sl = ifvg.bottom if smt_direction == Direction.BULLISH else ifvg.top
        risk = abs(entry - sl)
        tp = entry + 2 * risk if smt_direction == Direction.BULLISH else entry - 2 * risk

        alerts.append(Alert(
            key=f"strat8|{symbol}|{c15[mss_idx].time.isoformat()}",
            strategy="SMT + MSS + IFVG (Strategy 8)",
            symbol=symbol,
            direction="buy" if smt_direction == Direction.BULLISH else "sell",
            entry_type="limit",
            entry=entry, sl=sl, tp=tp,
            note=f"SMT divergence vs {correlated}, confirmed by structure break + inversion FVG.",
        ))
    return alerts


def smt_mss_breaker_strategy(data: dict) -> list[Alert]:
    alerts = []
    for symbol in ALL_SYMBOLS:
        c15 = data.get((symbol, "15M"), [])
        correlated = CORRELATED_PAIR[symbol]
        corr15 = data.get((correlated, "15M"), [])
        if not c15 or not corr15:
            continue

        smt_direction = detect_smt_divergence(c15, corr15, lookback=30)
        if smt_direction is None:
            continue

        swings = find_swings(c15, width=1)
        ref_swing = last_swing_before(swings, len(c15) - 1, is_high=(smt_direction == Direction.BEARISH))
        if ref_swing is None:
            continue
        mss_idx = _close_beyond(c15, ref_swing.price, ref_swing.index + 1, smt_direction)
        if mss_idx is None:
            continue

        order_blocks = find_all_order_blocks(c15[: mss_idx + 1])
        breakers = find_breaker_blocks(c15, order_blocks)
        matching = [b for b in breakers if b.direction == smt_direction and b.invalidated_at_index <= mss_idx]
        if not matching:
            continue
        breaker = matching[-1]

        entry = (breaker.top + breaker.bottom) / 2
        sl = breaker.bottom if smt_direction == Direction.BULLISH else breaker.top
        risk = abs(entry - sl)
        tp = entry + 2 * risk if smt_direction == Direction.BULLISH else entry - 2 * risk

        alerts.append(Alert(
            key=f"strat9|{symbol}|{c15[mss_idx].time.isoformat()}",
            strategy="SMT + MSS + Breaker Block (Strategy 9)",
            symbol=symbol,
            direction="buy" if smt_direction == Direction.BULLISH else "sell",
            entry_type="limit",
            entry=entry, sl=sl, tp=tp,
            note=f"SMT divergence vs {correlated}, confirmed by structure break + a flipped (breaker) order block.",
        ))
    return alerts


# ---------------------------------------------------------------------------
# 10. "Liquidity Sweep + MSS + Breaker Block + FVG" (Strategy 10)
# Session-agnostic, highest-conviction combo - requires BOTH a breaker
# block AND an FVG in the same direction after the MSS.
# ---------------------------------------------------------------------------

def liquidity_sweep_mss_breaker_fvg_strategy(data: dict) -> list[Alert]:
    alerts = []
    for symbol in ALL_SYMBOLS:
        c15 = data.get((symbol, "15M"), [])
        if not c15:
            continue
        result = _generic_sweep_and_mss(c15)
        if result is None:
            continue
        working, sweep_idx, mss_idx, direction = result

        order_blocks = find_all_order_blocks(working[: mss_idx + 1])
        breakers = find_breaker_blocks(working, order_blocks)
        matching_breakers = [b for b in breakers if b.direction == direction and b.invalidated_at_index <= mss_idx]
        if not matching_breakers:
            continue

        fvgs = find_fvgs(working, start_index=max(mss_idx - 2, 0))
        mark_mitigated(fvgs, working)
        gap = unmitigated_fvg_in_range(fvgs, working, mss_idx, len(working) - 1, direction)
        if gap is None:
            continue

        breaker = matching_breakers[-1]
        entry = gap.midpoint
        sl = min(gap.bottom, breaker.bottom) if direction == Direction.BULLISH else max(gap.top, breaker.top)
        risk = abs(entry - sl)
        tp = entry + 2 * risk if direction == Direction.BULLISH else entry - 2 * risk

        alerts.append(Alert(
            key=f"strat10|{symbol}|{working[gap.formed_at_index].time.isoformat()}",
            strategy="Liquidity Sweep + MSS + Breaker Block + FVG (Strategy 10)",
            symbol=symbol,
            direction="buy" if direction == Direction.BULLISH else "sell",
            entry_type="limit",
            entry=entry, sl=sl, tp=tp,
            note="Highest-conviction combo - both a breaker block AND an FVG align after the sweep+MSS.",
        ))
    return alerts



# ---------------------------------------------------------------------------
# 11. "ORB Breakout + Retest + FVG" (Strategy 11)
# Mark the 9:30 AM NY 15M candle's high/low as the opening range. On a 5M
# close beyond either side, wait for a retest into an FVG that formed in
# the breakout direction. Entry = FVG midpoint (pending limit), SL = FVG's
# far edge, TP = 2R.
# ---------------------------------------------------------------------------

def orb_breakout_retest_fvg_strategy(data: dict) -> list[Alert]:
    alerts = []
    for symbol in ALL_SYMBOLS:
        c15 = data.get((symbol, "15M"), [])
        c5 = data.get((symbol, "5M"), [])
        if not c15 or not c5:
            continue

        # Find the 9:30 AM NY 15M candle (opening range for US equities/CFDs)
        orb_candle = None
        for c in c15:
            t_ny = c.time.astimezone(NY_TZ)
            if t_ny.hour == 9 and t_ny.minute == 30:
                orb_candle = c
                break
        if orb_candle is None:
            continue

        range_high = orb_candle.high
        range_low = orb_candle.low
        post = [c for c in c5 if c.time > orb_candle.time]
        if len(post) < 5:
            continue

        # Look for first 5M close beyond the range
        breakout_idx = None
        direction = None
        for i, c in enumerate(post):
            if c.close > range_high:
                breakout_idx = i
                direction = Direction.BULLISH
                break
            if c.close < range_low:
                breakout_idx = i
                direction = Direction.BEARISH
                break
        if breakout_idx is None:
            continue

        # FVG formed in the breakout direction after the breakout.
        # We intentionally do NOT require the FVG to still be unmitigated —
        # the retest that confirms the setup is expected to touch the gap.
        fvgs = find_fvgs(post, start_index=max(breakout_idx - 1, 0))
        candidates = [g for g in fvgs if g.direction == direction
                      and breakout_idx <= g.formed_at_index]
        if not candidates:
            continue
        gap = candidates[-1]

        # Retest: price has tapped the FVG after it formed
        tapped = False
        for c in post[gap.formed_at_index + 1:]:
            if c.low <= gap.top and c.high >= gap.bottom:
                tapped = True
                break
        if not tapped:
            continue

        entry = gap.midpoint
        sl = gap.bottom if direction == Direction.BULLISH else gap.top
        risk = abs(entry - sl)
        if risk <= 0:
            continue
        tp = entry + 2 * risk if direction == Direction.BULLISH else entry - 2 * risk

        alerts.append(Alert(
            key=f"strat11|{symbol}|{post[gap.formed_at_index].time.isoformat()}",
            strategy="ORB Breakout + Retest + FVG (Strategy 11)",
            symbol=symbol,
            direction="buy" if direction == Direction.BULLISH else "sell",
            entry_type="limit",
            entry=entry, sl=sl, tp=tp,
            note="9:30 NY opening-range breakout, retest into FVG formed in breakout direction.",
        ))
    return alerts


# ---------------------------------------------------------------------------
# 12. "4H Range False Breakout / CRT" (Strategy 12)
# Mark the first 4H candle of the trading day (day boundary in New York
# time) as the range. If a 5M candle closes beyond the range, then a later
# 5M candle closes back inside it, that's a false breakout — enter in the
# reversal direction (market order) at that close. SL = the breakout
# candle's wick extreme. TP = the opposite side of the range.
# ---------------------------------------------------------------------------

def four_h_range_false_breakout_strategy(data: dict) -> list[Alert]:
    alerts = []
    for symbol in ALL_SYMBOLS:
        c4h = data.get((symbol, "4H"), [])
        c5 = data.get((symbol, "5M"), [])
        if not c4h or not c5:
            continue

        now = utc_now()
        # First 4H candle of the current NY trading day (00:00 NY)
        today_ny = now.astimezone(NY_TZ).date()
        range_candle = None
        for c in c4h:
            t_ny = c.time.astimezone(NY_TZ)
            if t_ny.date() == today_ny and t_ny.hour == 0 and t_ny.minute == 0:
                range_candle = c
                break
        if range_candle is None:
            # Fallback: most recent 4H candle that opened at 00:00 NY
            for c in reversed(c4h):
                t_ny = c.time.astimezone(NY_TZ)
                if t_ny.hour == 0 and t_ny.minute == 0:
                    range_candle = c
                    break
        if range_candle is None:
            continue

        range_high = range_candle.high
        range_low = range_candle.low
        post = [c for c in c5 if c.time > range_candle.time]
        if len(post) < 5:
            continue

        # Find a close beyond the range, then a later close back inside
        breakout_idx = None
        direction = None  # direction of the false-breakout reversal
        close_idx = None
        for i, c in enumerate(post):
            if c.close > range_high:
                for j in range(i + 1, len(post)):
                    if post[j].close < range_high and post[j].close > range_low:
                        breakout_idx = i
                        direction = Direction.BEARISH  # fade the upside false break
                        close_idx = j
                        break
                if breakout_idx is not None:
                    break
            elif c.close < range_low:
                for j in range(i + 1, len(post)):
                    if post[j].close > range_low and post[j].close < range_high:
                        breakout_idx = i
                        direction = Direction.BULLISH
                        close_idx = j
                        break
                if breakout_idx is not None:
                    break
        if breakout_idx is None or close_idx is None:
            continue

        breakout_candle = post[breakout_idx]
        entry = post[close_idx].close
        if direction == Direction.BEARISH:
            sl = breakout_candle.high
            tp = range_low
        else:
            sl = breakout_candle.low
            tp = range_high

        risk = abs(entry - sl)
        if risk <= 0:
            continue

        alerts.append(Alert(
            key=f"strat12|{symbol}|{post[close_idx].time.isoformat()}",
            strategy="4H Range False Breakout / CRT (Strategy 12)",
            symbol=symbol,
            direction="buy" if direction == Direction.BULLISH else "sell",
            entry_type="market",
            entry=entry, sl=sl, tp=tp,
            note="False breakout of the first 4H candle of the NY day — enter on close back inside the range.",
        ))
    return alerts


# ---------------------------------------------------------------------------
# 13. "Breakout + Momentum + Chandelier Exit" (Strategy 13)
# Detect a consolidation range (recent N candles contained within roughly
# 2.5x ATR). On a breakout candle with body > 1x ATR (or 3 consecutive
# same-direction candles closing beyond the range), enter at close (market).
# SL = the consolidation's far edge. TP1 = 1.5R (partial). Trail remainder
# with Chandelier Exit instructions placed in the alert note only (bot does
# not manage open trades).
# ---------------------------------------------------------------------------

def breakout_momentum_chandelier_strategy(data: dict) -> list[Alert]:
    alerts = []
    for symbol in ALL_SYMBOLS:
        c15 = data.get((symbol, "15M"), [])
        if not c15 or len(c15) < 40:
            continue

        current_atr = atr(c15, period=14)
        if current_atr is None or current_atr <= 0:
            continue

        # Look for a recent consolidation: last 8-12 candles range <= 2.5 * ATR
        lookback = 10
        if len(c15) < lookback + 5:
            continue
        consol = c15[-(lookback + 5):-5]
        consol_high = max(c.high for c in consol)
        consol_low = min(c.low for c in consol)
        consol_range = consol_high - consol_low
        if consol_range > 2.5 * current_atr:
            continue

        # Breakout: last candle body > 1x ATR and closes beyond consol, OR
        # 3 consecutive same-direction closes beyond the range
        recent = c15[-5:]
        direction = None
        entry_candle = None

        last = recent[-1]
        body = abs(last.close - last.open)
        if body >= current_atr:
            if last.close > consol_high and last.is_bullish:
                direction = Direction.BULLISH
                entry_candle = last
            elif last.close < consol_low and last.is_bearish:
                direction = Direction.BEARISH
                entry_candle = last

        if direction is None:
            # 3 consecutive
            if (all(c.close > consol_high for c in recent[-3:]) and
                    all(c.is_bullish for c in recent[-3:])):
                direction = Direction.BULLISH
                entry_candle = recent[-1]
            elif (all(c.close < consol_low for c in recent[-3:]) and
                  all(c.is_bearish for c in recent[-3:])):
                direction = Direction.BEARISH
                entry_candle = recent[-1]

        if direction is None or entry_candle is None:
            continue

        entry = entry_candle.close
        if direction == Direction.BULLISH:
            sl = consol_low
            risk = entry - sl
            tp1 = entry + 1.5 * risk
        else:
            sl = consol_high
            risk = sl - entry
            tp1 = entry - 1.5 * risk

        if risk <= 0:
            continue

        trail_note = (
            "TP1 = 1.5R (partial). After TP1, trail remainder with Chandelier Exit: "
            "for longs use highest-high-since-entry minus (ATR × 2); for shorts use "
            "lowest-low-since-entry plus (ATR × 2). Exit early on a colour change "
            "against the position. Bot does not manage the trade after alert."
        )

        alerts.append(Alert(
            key=f"strat13|{symbol}|{entry_candle.time.isoformat()}",
            strategy="Breakout + Momentum + Chandelier Exit (Strategy 13)",
            symbol=symbol,
            direction="buy" if direction == Direction.BULLISH else "sell",
            entry_type="market",
            entry=entry, sl=sl, tp=tp1,
            note=trail_note,
        ))
    return alerts


# ---------------------------------------------------------------------------
# 14. "8AM 1H Range Sweep + IFVG + OB" (Strategy 14)
# Mark the 8:00 AM NY 1H candle's high/low as the range. Wait for a later
# 1M/5M candle to sweep one side, then form an Inversion FVG, then find a
# confirmed order block near it. Entry = market at the candle after the
# IFVG. SL = the causal swing. TP = opposite side of the 8AM range.
# ---------------------------------------------------------------------------

def eight_am_sweep_ifvg_ob_strategy(data: dict) -> list[Alert]:
    alerts = []
    for symbol in ALL_SYMBOLS:
        c1h = data.get((symbol, "1H"), [])
        # Prefer 1M, fall back to 5M
        c_fine = data.get((symbol, "1M"), []) or data.get((symbol, "5M"), [])
        if not c1h or not c_fine:
            continue

        # 8:00 AM NY 1H candle
        eight_am = None
        for c in c1h:
            t_ny = c.time.astimezone(NY_TZ)
            if t_ny.hour == 8 and t_ny.minute == 0:
                eight_am = c
                break
        if eight_am is None:
            continue

        range_high = eight_am.high
        range_low = eight_am.low
        post = [c for c in c_fine if c.time > eight_am.time]
        if len(post) < 10:
            continue

        # Sweep of one side of the range
        swept_high = False
        swept_low = False
        sweep_idx = None
        for i, c in enumerate(post):
            if c.high > range_high:
                swept_high = True
                sweep_idx = i
                break
            if c.low < range_low:
                swept_low = True
                sweep_idx = i
                break
        if sweep_idx is None:
            continue

        # Direction after sweep
        if swept_high:
            direction = Direction.BEARISH
        else:
            direction = Direction.BULLISH

        # Find IFVG after the sweep
        fvgs = find_fvgs(post, start_index=max(sweep_idx, 0))
        mark_mitigated(fvgs, post)
        ifvg = detect_ifvg(fvgs, post, from_index=sweep_idx)
        if ifvg is None:
            continue

        ob = find_order_block(post, ifvg.formed_at_index, direction)
        if ob is None or not ob.confirmed:
            continue

        entry_idx = min(ifvg.formed_at_index + 1, len(post) - 1)
        entry = post[entry_idx].close

        swings = find_swings(post[: ifvg.formed_at_index + 1], width=1)
        ref_swing = last_swing_before(swings, ifvg.formed_at_index, is_high=(direction == Direction.BEARISH))
        if ref_swing is None:
            continue
        sl = ref_swing.price
        tp = range_low if direction == Direction.BEARISH else range_high

        alerts.append(Alert(
            key=f"strat14|{symbol}|{post[ifvg.formed_at_index].time.isoformat()}",
            strategy="8AM 1H Range Sweep + IFVG + OB (Strategy 14)",
            symbol=symbol,
            direction="buy" if direction == Direction.BULLISH else "sell",
            entry_type="market",
            entry=entry, sl=sl, tp=tp,
            note="8AM NY 1H range swept, IFVG + confirmed order block. TP is the opposite side of the 8AM range.",
        ))
    return alerts


ALL_STRATEGIES = [
    asians_strategy,
    london_tt_strategy,
    asian_tt_strategy,
    newyork_strategy,
    newyork_tt_strategy,
    liquidity_sweep_mss_fvg_strategy,
    liquidity_sweep_bpr_strategy,
    smt_mss_ifvg_strategy,
    smt_mss_breaker_strategy,
    liquidity_sweep_mss_breaker_fvg_strategy,
    orb_breakout_retest_fvg_strategy,
    four_h_range_false_breakout_strategy,
    breakout_momentum_chandelier_strategy,
    eight_am_sweep_ifvg_ob_strategy,
]
