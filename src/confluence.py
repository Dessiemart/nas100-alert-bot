"""
Partial-confluence progress tracking for the new SMC modules in
src/strategies_smc.py. Mirrors each strategy step-for-step so a "X/Y
confluences" heads-up fires BEFORE the full setup, same pattern as
src/confluence.py.
"""

from datetime import timedelta

import config
from src.candles import (
    Direction, find_swings, last_swing_before, find_fvgs, mark_mitigated,
    unmitigated_fvg_in_range, find_order_block, detect_ifvg, structure_trend, utc_now,
)
from src.killzones import NY_TZ
from src.strategies import _close_beyond, ALL_SYMBOLS
from src.confluence import ConfluenceCheck
from src.strategies_smc import (
    _fvg_overlapping_level, _one_min_reversal, _hourly_key_level_tapped,
)


# ---------------------------------------------------------------------------
# 1. ORB Breakout + Retest + FVG
# ---------------------------------------------------------------------------

def check_orb_progress(data: dict) -> list[ConfluenceCheck]:
    results: list[ConfluenceCheck] = []
    for symbol in ALL_SYMBOLS:
        results += _check_orb_for_symbol(symbol, data)
    return results


def _check_orb_for_symbol(symbol: str, data: dict) -> list[ConfluenceCheck]:
    c15 = data.get((symbol, "15M"), [])
    c5 = data.get((symbol, "5M"), [])
    c1 = data.get((symbol, "1M"), [])
    names = ["ORB High/Low marked (9:30 15m candle)",
             "5M close beyond ORB level",
             "Retest into ORB level / overlapping FVG",
             "1M CHoCH / IFVG / OB reversal"]
    if not c15 or not c5:
        return []
    orb_candle = next(
        (c for c in c15 if (t := c.time.astimezone(NY_TZ)).hour == 9 and t.minute == 30),
        None,
    )
    r0 = orb_candle is not None
    if not r0:
        return [ConfluenceCheck("ORB", symbol, "unknown", names, [False] * 4)]
    orb_high, orb_low = orb_candle.high, orb_candle.low
    post = [c for c in c5 if c.time >= orb_candle.time + timedelta(minutes=15)]
    tol = orb_high * 1e-5

    out = []
    for direction, level in ((Direction.BULLISH, orb_high), (Direction.BEARISH, orb_low)):
        dir_label = "buy" if direction == Direction.BULLISH else "sell"
        r = [True, False, False, False]
        break_idx = _close_beyond(post, level, 1, direction) if post else None
        r[1] = break_idx is not None
        if break_idx is not None:
            fvgs = find_fvgs(post, start_index=break_idx)
            mark_mitigated(fvgs, post)
            has_zone = bool(_fvg_overlapping_level(fvgs, level, direction, tol)) or any(
                post[i].low <= level <= post[i].high for i in range(break_idx + 1, len(post)))
            r[2] = has_zone
            if has_zone and c1:
                c1_from = [i for i, c in enumerate(c1) if c.time >= post[break_idx].time]
                if c1_from:
                    r[3] = _one_min_reversal(c1, c1_from[0], direction) is not None
        out.append(ConfluenceCheck("ORB breakout+retest", symbol, dir_label, names, r))
    return out


# ---------------------------------------------------------------------------
# 2. 4H Range False Breakout / CRT Sweep
# ---------------------------------------------------------------------------

def check_fourh_fb_progress(data: dict) -> list[ConfluenceCheck]:
    results: list[ConfluenceCheck] = []
    names = ["4H range marked (first 4H candle of day)",
             "5M close beyond range", "Next 5M close back inside (entry trigger)"]
    for symbol in ALL_SYMBOLS:
        c4h = data.get((symbol, "4H"), [])
        c5 = data.get((symbol, "5M"), [])
        r = [False, False, False]
        if not c4h or not c5:
            continue
        first_4h = next((c for c in reversed(c4h)
                         if c.time.astimezone(NY_TZ).hour in (0, 4, 8, 12, 16, 20)), None)
        r[0] = first_4h is not None
        if first_4h is None:
            results.append(ConfluenceCheck("4H range FB", symbol, "unknown", names, r))
            continue
        rh, rl = first_4h.high, first_4h.low
        post = [c for c in c5 if c.time >= first_4h.time + timedelta(hours=4)]
        for swept_low, direction in ((True, Direction.BULLISH), (False, Direction.BEARISH)):
            rr = list(r)
            beyond = (lambda c: c.close < rl) if swept_low else (lambda c: c.close > rh)
            back_in = (lambda c: c.close > rl) if swept_low else (lambda c: c.close < rh)
            for i in range(len(post) - 1):
                if beyond(post[i]):
                    rr[1] = True
                    if back_in(post[i + 1]):
                        rr[2] = True
                    break
            dir_label = "buy" if direction == Direction.BULLISH else "sell"
            results.append(ConfluenceCheck("4H range FB", symbol, dir_label, names, rr))
    return results


# ---------------------------------------------------------------------------
# 3. Range Sweep + IFVG + OB
# ---------------------------------------------------------------------------

def check_range_sweep_progress(data: dict) -> list[ConfluenceCheck]:
    results: list[ConfluenceCheck] = []
    names = ["8AM 1H range marked", "Range side swept (wick)", "Key level tapped (hourly swing/equal lows/FVG)",
             "1M IFVG formed (close back through FVG)", "OB confirmed"]
    for symbol in ALL_SYMBOLS:
        c1h = data.get((symbol, "1H"), [])
        c1 = data.get((symbol, "1M"), [])
        r = [False] * 5
        if not c1h or not c1:
            continue
        range_candle = next((c for c in c1h if c.time.astimezone(NY_TZ).hour == 8), None)
        r[0] = range_candle is not None
        if range_candle is None:
            results.append(ConfluenceCheck("Range sweep", symbol, "unknown", names, r))
            continue
        rh, rl = range_candle.high, range_candle.low
        later = [c for c in c1h if c.time > range_candle.time]
        if not later:
            results.append(ConfluenceCheck("Range sweep", symbol, "unknown", names, r))
            continue
        working = [c for c in c1 if c.time >= later[0].time]
        for swept_low, level, direction in ((True, rl, Direction.BULLISH), (False, rh, Direction.BEARISH)):
            rr = list(r)
            dir_label = "buy" if direction == Direction.BULLISH else "sell"
            sweep_idx = next(
                (i for i, c in enumerate(working) if (c.low < level if swept_low else c.high > level)),
                None,
            )
            if sweep_idx is None:
                results.append(ConfluenceCheck("Range sweep", symbol, dir_label, names, rr))
                continue
            rr[1] = True
            sweep_price = working[sweep_idx].low if swept_low else working[sweep_idx].high
            rr[2] = _hourly_key_level_tapped(c1h, working[sweep_idx].time, swept_low, sweep_price)
            fvgs = find_fvgs(working, start_index=max(sweep_idx - 2, 0))
            mark_mitigated(fvgs, working)
            ifvg = detect_ifvg(fvgs, working, from_index=sweep_idx + 1) if rr[2] else None
            rr[3] = ifvg is not None
            if ifvg is not None:
                ob = find_order_block(working, ifvg.formed_at_index, direction)
                rr[4] = ob is not None and ob.confirmed
            results.append(ConfluenceCheck("Range sweep IFVG OB", symbol, dir_label, names, rr))
    return results


# ---------------------------------------------------------------------------
# 4. Daily Profile Session Bias (informational - 2 steps)
# ---------------------------------------------------------------------------

def check_session_bias_progress(data: dict) -> list[ConfluenceCheck]:
    from src.strategies_smc import daily_session_bias
    results = []
    names = ["Asia range marked", "London sweep/distribution read"]
    for symbol in ("EURUSD", "NAS100", "XAUUSD"):
        c5 = data.get((symbol, "5M"), [])
        asia = [c for c in c5 if (t := c.time.astimezone(NY_TZ)).hour >= 20] if c5 else []
        london = [c for c in c5 if 2 <= (t := c.time.astimezone(NY_TZ)).hour < 5] if c5 else []
        r = [bool(asia), bool(london)]
        if all(r):
            results.append(ConfluenceCheck(
                "Session bias", symbol, daily_session_bias(data, symbol), names, r))
    return results


# ---------------------------------------------------------------------------
# 5. SMT Divergence
# ---------------------------------------------------------------------------

def check_smt_progress(data: dict, anchor: str = "NAS100",
                       comparison: str = "EURUSD") -> list[ConfluenceCheck]:
    from src.strategies_smc import smt_divergence_strategy
    names = ["Divergence at swing lows/highs", "Not stale (<6h old)",
             "MSS (close beyond causal swing)", "FVG / OB after MSS"]
    a5 = data.get((anchor, "5M"), [])
    c5 = data.get((comparison, "5M"), [])
    r = [False, False, False, False]
    if len(a5) < 10 or len(c5) < 10:
        return [ConfluenceCheck("SMT", anchor, "unknown", names, r)]
    swings_a = [s for s in find_swings(a5, width=1) if not s.is_high][-2:]
    swings_c = [s for s in find_swings(c5, width=1) if not s.is_high][-2:]
    if len(swings_a) >= 2 and len(swings_c) >= 2:
        r[0] = (swings_a[-1].price < swings_a[-2].price and swings_c[-1].price > swings_c[-2].price) or \
               (swings_a[-1].price > swings_a[-2].price and swings_c[-1].price < swings_c[-2].price)
        if r[0]:
            r[1] = (utc_now() - swings_a[-1].time).total_seconds() <= 6 * 3600
    fired = smt_divergence_strategy(data, anchor, comparison)
    if fired:
        r[2] = r[3] = True
        return [ConfluenceCheck("SMT divergence", anchor, fired[0].direction, names, r)]
    direction = Direction.BULLISH if fired and fired[0].direction == "buy" else Direction.BEARISH
    return [ConfluenceCheck("SMT", anchor,
                            "buy" if r[0] and swings_a[-1].price < swings_a[-2].price else "sell",
                            names, r)]


# ---------------------------------------------------------------------------
# 6. Breakout + Momentum
# ---------------------------------------------------------------------------

def check_breakout_momentum_progress(data: dict) -> list[ConfluenceCheck]:
    results: list[ConfluenceCheck] = []
    names = ["Consolidation marked (>=2 touches of S/R)", "Momentum candle closes beyond level"]
    for symbol in ALL_SYMBOLS:
        c5 = data.get((symbol, "5M"), [])
        r = [False, False]
        if len(c5) < 20:
            continue
        tol = c5[-1].close * getattr(config, "TOUCH_TOLERANCE_PCT", 2e-4)
        swings = find_swings(c5, width=2)
        for is_high, direction in ((True, Direction.BULLISH), (False, Direction.BEARISH)):
            pts = [s.price for s in swings if s.is_high == is_high]
            rr = list(r)
            dir_label = "buy" if direction == Direction.BULLISH else "sell"
            for i in range(len(pts)):
                for j in range(i + 1, len(pts)):
                    if abs(pts[i] - pts[j]) <= tol:
                        rr[0] = True
                        level = max(pts[i], pts[j]) if is_high else min(pts[i], pts[j])
                        beyond = (lambda c: c.close > level) if is_high else (lambda c: c.close < level)
                        body_ok = (getattr(config, "MOMENTUM_BODY_PCT", 0.60),)
                        for c in c5[::-1]:
                            if beyond(c):
                                big = c.body_pct_of_range >= body_ok[0]
                                rr[1] = big or rr[1]
                                break
                        break
                if rr[0]:
                    break
            results.append(ConfluenceCheck("Breakout momentum", symbol, dir_label, names, rr))
    return results


ALL_SMC_PROGRESS_CHECKERS = [
    check_orb_progress,
    check_fourh_fb_progress,
    check_range_sweep_progress,
    check_session_bias_progress,
    check_smt_progress,
    check_breakout_momentum_progress,
]
