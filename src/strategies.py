"""
The five mechanical strategies, one function each. Every function takes
the `data` dict produced by ctrader_client.fetch_all_trendbars (keyed by
(symbol, period_label) -> list[Candle]) and returns a list of Alert
objects - empty if no valid setup exists right now.

Each function is a direct translation of the plain-English rules we
locked in while defining the strategy (see /areas/nasdaq-alert-system.md).
Two honest gaps, flagged rather than silently guessed at:

  1. "Asians" never had explicit SL/TP rules defined in our conversation
     - only the entry trigger was formalized. This code uses a sensible
     default (SL beyond the zone that triggered entry, TP at 2R) marked
     clearly below. Revisit this if you want different SL/TP handling.
  2. All "reaction candle" and "zone tap" checks scan forward from the
     triggering event using the candle data available in a single fetch
     window - if a setup's trigger candle has already scrolled out of the
     fetched window by the time a run happens, it will be missed. This is
     a v1 tradeoff for staying inside GitHub Actions' free short-run
     budget; the backtesting stage (Stage 13) is exactly where this gets
     tuned.
"""

from dataclasses import dataclass, field
from datetime import datetime

import config
from src.candles import (
    Candle, Direction, find_swings, last_swing_before, find_fvgs, mark_mitigated,
    unmitigated_fvg_in_range, find_order_block, detect_ifvg, structure_trend, utc_now,
)
from src.killzones import ASIAN, LONDON, NEW_YORK_AM, pre_london_window


@dataclass
class Alert:
    key: str            # unique id for dedup across runs
    strategy: str
    symbol: str
    direction: str       # "buy" or "sell"
    entry_type: str      # "market", "limit", "buy-stop", "sell-stop"
    entry: float
    sl: float
    tp: float
    note: str = ""


def _close_beyond(candles: list[Candle], level: float, from_index: int, direction: Direction) -> int | None:
    """First index >= from_index whose CLOSE breaks beyond `level` in the
    given direction. Wicks don't count."""
    for i in range(from_index, len(candles)):
        c = candles[i]
        if direction == Direction.BULLISH and c.close > level:
            return i
        if direction == Direction.BEARISH and c.close < level:
            return i
    return None


def _zone_tap(candles: list[Candle], top: float, bottom: float, from_index: int) -> int | None:
    for i in range(from_index, len(candles)):
        c = candles[i]
        if c.low <= top and c.high >= bottom:
            return i
    return None


def _reaction_candle(candles: list[Candle], from_index: int, direction: Direction) -> int | None:
    for i in range(from_index, len(candles)):
        c = candles[i]
        if direction == Direction.BULLISH and c.is_bullish:
            return i
        if direction == Direction.BEARISH and c.is_bearish:
            return i
    return None


# ---------------------------------------------------------------------------
# 1. "Asians" - Asian killzone sweep + 1H trend continuation/reversal (NAS100)
# ---------------------------------------------------------------------------

def asians_strategy(data: dict) -> list[Alert]:
    symbol = "NAS100"
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


def _asians_continuation(symbol, working, sweep_idx, direction: Direction, swept_level) -> Alert | None:
    # 5M BOS in trend direction: close beyond most recent opposite swing
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

    tap_idx = _zone_tap(working, gap.top, gap.bottom, gap.formed_at_index + 1)
    if tap_idx is None:
        return None

    reaction_idx = _reaction_candle(working, tap_idx, direction)
    if reaction_idx is None:
        return None

    reaction = working[reaction_idx]
    entry = reaction.close
    sl = gap.bottom if direction == Direction.BULLISH else gap.top
    risk = abs(entry - sl)
    tp = entry + 2 * risk if direction == Direction.BULLISH else entry - 2 * risk

    return Alert(
        key=f"asians|{symbol}|continuation|{reaction.time.isoformat()}",
        strategy="Asians (continuation)",
        symbol=symbol,
        direction="buy" if direction == Direction.BULLISH else "sell",
        entry_type="market",
        entry=entry, sl=sl, tp=tp,
        note="SL/TP default (2R) - 'Asians' never had explicit SL/TP rules defined, confirm before trusting these levels.",
    )


def _asians_reversal(symbol, c15, working, sweep_idx, direction: Direction, swept_level, swept_is_high) -> Alert | None:
    # 15M FVG/OB above/below the swept area
    fvgs_15 = find_fvgs(c15)
    mark_mitigated(fvgs_15, c15)
    zone_direction = Direction.BEARISH if swept_is_high else Direction.BULLISH
    candidate_zones = [g for g in fvgs_15 if g.direction == zone_direction and not g.mitigated]
    if not candidate_zones:
        return None
    zone = candidate_zones[-1]

    # 5M CHoCH: close beyond the swing that supported the sweep
    swings = find_swings(working[: sweep_idx + 1], width=1)
    ref_swing = last_swing_before(swings, sweep_idx, is_high=not swept_is_high)
    if ref_swing is None:
        return None
    choch_idx = _close_beyond(working, ref_swing.price, sweep_idx + 1, direction)
    if choch_idx is None:
        return None

    tap_idx = _zone_tap(working, zone.top, zone.bottom, choch_idx)
    if tap_idx is None:
        return None

    reaction_idx = _reaction_candle(working, tap_idx, direction)
    if reaction_idx is None:
        return None

    reaction = working[reaction_idx]
    entry = reaction.close
    sl = zone.bottom if direction == Direction.BULLISH else zone.top
    risk = abs(entry - sl)
    tp = entry + 2 * risk if direction == Direction.BULLISH else entry - 2 * risk

    return Alert(
        key=f"asians|{symbol}|reversal|{reaction.time.isoformat()}",
        strategy="Asians (reversal)",
        symbol=symbol,
        direction="buy" if direction == Direction.BULLISH else "sell",
        entry_type="market",
        entry=entry, sl=sl, tp=tp,
        note="SL/TP default (2R) - 'Asians' never had explicit SL/TP rules defined, confirm before trusting these levels.",
    )


# ---------------------------------------------------------------------------
# 2. "London tt" - Pre-London Pullback -> London Continuation (NAS100 + Gold)
# ---------------------------------------------------------------------------

def london_tt_strategy(data: dict) -> list[Alert]:
    alerts: list[Alert] = []
    for symbol in ("NAS100", "XAUUSD"):
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

        asian_high = max(c.high for c in [c for c in c5 if asian_start <= c.time < asian_end]) if c5 else None
        asian_low = min(c.low for c in [c for c in c5 if asian_start <= c.time < asian_end]) if c5 else None
        if asian_high is None or asian_low is None:
            continue
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
            continue  # pullback too shallow

        # Confirmation candles at/after London killzone start
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
        tp = entry + risk if direction == Direction.BULLISH else entry - risk  # 1:1 minimum

        alerts.append(Alert(
            key=f"london_tt|{symbol}|{confirm_15.time.isoformat()}",
            strategy="London tt",
            symbol=symbol,
            direction="buy" if direction == Direction.BULLISH else "sell",
            entry_type="buy-stop" if direction == Direction.BULLISH else "sell-stop",
            entry=entry, sl=sl, tp=tp,
            note="TP shown is 1:1 minimum - check 15M/1H for a nearer untapped swing to extend the target.",
        ))
    return alerts


# ---------------------------------------------------------------------------
# 3. "Asian tt" - Asian Sweep -> CHOCH -> 50% FVG entry (NAS100)
# ---------------------------------------------------------------------------

def asian_tt_strategy(data: dict) -> list[Alert]:
    symbol = "NAS100"
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

    # If both sides confirmed, only take whichever CHOCH happened first.
    results.sort(key=lambda r: r[0])
    choch_idx, direction, gap, ref_swing, opposite_extreme_level = results[0]

    entry = gap.midpoint
    sl = ref_swing.price
    tp = asian_low if direction == Direction.BEARISH else asian_high

    return [Alert(
        key=f"asian_tt|{symbol}|{post_asian[choch_idx].time.isoformat()}",
        strategy="Asian tt",
        symbol=symbol,
        direction="buy" if direction == Direction.BULLISH else "sell",
        entry_type="limit",
        entry=entry, sl=sl, tp=tp,
        note="Pending limit order, no expiry per your rule - leave it working until filled or session invalidates it.",
    )]


# ---------------------------------------------------------------------------
# 4 & 5. "newyork" / "newyork tt" - 9 AM Candle Range Model
# ---------------------------------------------------------------------------

def _newyork_cr(symbol: str, data: dict, sl_buffer: float) -> list[Alert]:
    c1h = data.get((symbol, "1H"), [])
    c1m = data.get((symbol, "1M"), [])
    if not c1h or not c1m:
        return []

    now = utc_now()
    ny_start, ny_end = NEW_YORK_AM.current_or_most_recent_window(now)
    if now >= ny_end:
        return []  # NY AM killzone already over - setup expired

    # the 8:00 AM NY-time hourly candle
    eight_am_candle = next(
        (c for c in c1h if c.time.astimezone(__import__("src.killzones", fromlist=["NY_TZ"]).NY_TZ).hour == 8),
        None,
    )
    if eight_am_candle is None:
        return []
    range_high, range_low = eight_am_candle.high, eight_am_candle.low

    later_1h = [c for c in c1h if c.time > eight_am_candle.time]
    if not later_1h:
        return []
    next_hour = later_1h[0]

    if next_hour.close > range_high:
        direction = Direction.BEARISH  # high taken -> reversal short
        take_level = range_high
    elif next_hour.close < range_low:
        direction = Direction.BULLISH  # low taken -> reversal long
        take_level = range_low
    else:
        return []  # neither side closed beyond the range yet

    # Work on 1M candles from the break onward
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

    strategy_name = "newyork" if symbol == "NAS100" else "newyork tt"
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


ALL_STRATEGIES = [
    asians_strategy,
    london_tt_strategy,
    asian_tt_strategy,
    newyork_strategy,
    newyork_tt_strategy,
]
