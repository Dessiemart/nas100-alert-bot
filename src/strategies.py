"""
Ten mechanical strategies. Strategies 1-5 are the original set (renamed
with a bracketed number, logic unchanged from the proactive rewrite).
Strategies 6-10 are new, built from the "ChartTactix" specification:

  6. Liquidity Sweep + MSS + FVG        - session-agnostic version of
     the sweep+structure-break+FVG concept (not tied to Asian session)
  7. Liquidity Sweep + BPR              - sweep followed by a Balanced
     Price Range (overlapping opposite-direction FVG pair)
  8. SMT + MSS + IFVG                   - Smart Money Divergence vs a
     correlated instrument, confirmed by structure break + inversion FVG
  9. SMT + MSS + Breaker Block          - same SMT/MSS base, entry at a
     breaker block (an order block that failed and flipped role) instead
  10. Liquidity Sweep + MSS + Breaker Block + FVG - session-agnostic,
     requires BOTH a breaker block AND an FVG together (highest-
     conviction combo)

Strategies 8 and 9 need a correlated instrument's candles in `data`
(CORRELATED_PAIR below) - if that data isn't present, they return no
alert rather than fabricate an SMT signal, per project rule. Tested via
synthetic fixtures: all 10 run without crashing on generic/noisy data,
Strategy 6 fires with correctly-ordered levels on a genuine crafted
sweep+MSS+FVG setup, and Strategies 8/9 confirmed to produce ZERO
alerts when correlated pair data is missing.
"""

from dataclasses import dataclass, field
from datetime import datetime

import config
from src.candles import (
    Candle, Direction, find_swings, last_swing_before, find_fvgs, mark_mitigated,
    unmitigated_fvg_in_range, find_order_block, find_all_order_blocks, detect_ifvg,
    find_bpr_zones, find_breaker_blocks, detect_smt_divergence, structure_trend, utc_now,
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
]
