"""
New strategy modules merged from the "Trading Strategy Coding Spec" PDF
(ORB Breakout + Retest + SMC FVG / 4H Range False Breakout / 8AM 1H Range
Sweep / Daily Profile Session Bias / SMT Divergence / Liquidity Pool
Targets / Breakout + Momentum + Chandelier Exit / Top-Down MTF Framework).

They follow the same contract as src/strategies.py: each takes the `data`
dict keyed by (symbol, period_label) -> list[Candle] and returns a list of
Alert objects (empty when no valid setup exists right now).

Per the spec's "Coding AI Instructions":
  - Each strategy is an independent module.
  - Bar close only, no repainting (wicks count for sweeps, closes count
    for breaks - same convention as the existing modules).
  - Confidence comes from the number of confluences confirmed (see
    src/confluence_smc.py which mirrors every module step-for-step).

Add to config.py (defaults used here if missing):
    ORB_TP_R_MULTIPLE        = 2.0
    FOURH_FB_TP_MODE         = "opposite"   # "opposite" range side or "2R"
    MOMENTUM_BODY_PCT        = 0.60         # "big body" momentum candle
    MOMENTUM_TP1_R           = 1.5
    CHANDELIER_ATR_PERIOD    = 14
    CHANDELIER_ATR_MULT      = 2.0
    TOUCH_TOLERANCE_PCT      = 0.0002       # consolidation touch tolerance
    SMT_SWING_WIDTH          = 1
    KEYLEVEL_EQUAL_LO_TOLERANCE_PCT = 0.0003
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
    """FVGs of the given direction whose zone straddles `level`."""
    out = []
    for g in fvgs:
        if g.direction != direction or g.mitigated:
            continue
        if g.bottom - tol <= level <= g.top + tol:
            out.append(g)
    return out


def _one_min_reversal(c1: list[Candle], from_index: int, direction: Direction) -> int | None:
    """Index of the 1m candle where a bullish/bearish reversal confirms:
    close beyond the most recent opposite swing (CHoCH), an IFVG, or a
    confirmed OB. Returns None if none has happened yet."""
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


# ---------------------------------------------------------------------------
# 1. ORB Breakout + Retest + SMC FVG
#    First 15m candle after the 9:30 NYSE open = ORB High/Low.
#    Long: 5m close above ORB High -> retest into ORB High or bullish 5m
#    FVG overlapping ORB High -> 1m bullish reversal (CHoCH/IFVG/OB).
#    False-breakout variant: wick above ORB High but 5m close back inside
#    -> bearish 5m FVG overlapping ORB High -> short limit at the FVG.
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
    if not c15 or not c5 or not c1:
        return []

    # 9:30-9:45 NY candle
    orb_candle = next(
        (c for c in c15
         if (t := c.time.astimezone(NY_TZ)).hour == 9 and t.minute == 30),
        None,
    )
    if orb_candle is None:
        return []
    orb_high, orb_low = orb_candle.high, orb_candle.low
    post = [c for c in c5 if c.time >= orb_candle.time + timedelta(minutes=15)]
    if len(post) < 2:
        return []

    tp_r = _cfg("ORB_TP_R_MULTIPLE", 2.0)
    tol = orb_high * 1e-5
    alerts: list[Alert] = []

    # --- Trend continuation breakout (long side shown, short mirrored) ---
    for direction, orb_level, opposite in (
        (Direction.BULLISH, orb_high, orb_low),
        (Direction.BEARISH, orb_low, orb_high),
    ):
        break_idx = _close_beyond(post, orb_level, 1, direction)
        if break_idx is None:
            continue
        # Retest: a later 5m candle trades back into ORB level...
        retest_idx = next(
            (i for i in range(break_idx + 1, len(post))
             if post[i].low <= orb_level <= post[i].high),
            None,
        )
        # ...or a bullish/bearish 5m FVG overlapping ORB level
        fvgs = find_fvgs(post, start_index=break_idx)
        mark_mitigated(fvgs, post)
        zones = _fvg_overlapping_level(fvgs, orb_level, direction, tol)
        zone = zones[-1] if zones else None
        anchor_idx = retest_idx if retest_idx is not None else (
            zone.formed_at_index if zone is not None else None)
        if anchor_idx is None:
            continue
        # LTF confirmation: 1m bullish/bearish reversal after the break
        c1_from = [i for i, c in enumerate(c1) if c.time >= post[break_idx].time]
        if not c1_from:
            continue
        rev = _one_min_reversal(c1, c1_from[0], direction)
        if rev is None:
            continue

        entry = zone.midpoint if zone is not None else c1[rev].close
        sl = (zone.bottom if zone is not None else min(c.low for c in post[break_idx:anchor_idx + 1])) \
            if direction == Direction.BULLISH else \
            (zone.top if zone is not None else max(c.high for c in post[break_idx:anchor_idx + 1]))
        risk = abs(entry - sl)
        tp = entry + tp_r * risk if direction == Direction.BULLISH else entry - tp_r * risk
        side = "buy" if direction == Direction.BULLISH else "sell"
        alerts.append(Alert(
            key=f"orb|{symbol}|{side}|{post[anchor_idx].time.isoformat()}",
            strategy="ORB breakout+retest",
            symbol=symbol, direction=side,
            entry_type="limit" if zone is not None else "market",
            entry=entry, sl=sl, tp=tp,
            note=f"ORB {orb_low:.5g}-{orb_high:.5g}. "
                 + ("Limit at retest FVG." if zone is not None else "Market after 1m reversal.")
                 + " TP = 2R; consider opposing session low / liquidity pool instead.",
        ))

    # --- False breakout variant ---
    for swept_is_high in (True, False):
        level = orb_high if swept_is_high else orb_low
        direction = Direction.BEARISH if swept_is_high else Direction.BULLISH
        # wick beyond ORB level but 5m candle CLOSES back inside
        fake_idx = next(
            (i for i, c in enumerate(post)
             if ((c.high > level and c.close < level) if swept_is_high
                 else (c.low < level and c.close > level))),
            None,
        )
        if fake_idx is None:
            continue
        fvgs = find_fvgs(post, start_index=max(fake_idx - 2, 0))
        mark_mitigated(fvgs, post)
        zones = _fvg_overlapping_level(fvgs, level, direction, tol)
        if not zones:
            continue
        zone = zones[-1]
        # 1m reversal confirmation after the fake break
        c1_from = [i for i, c in enumerate(c1) if c.time >= post[fake_idx].time]
        if not c1_from or _one_min_reversal(c1, c1_from[0], direction) is None:
            continue

        entry = zone.midpoint
        sl = post[fake_idx].high if swept_is_high else post[fake_idx].low
        risk = abs(entry - sl)
        tp = entry - 2 * risk if swept_is_high else entry + 2 * risk
        side = "sell" if swept_is_high else "buy"
        alerts.append(Alert(
            key=f"orb_fb|{symbol}|{side}|{post[fake_idx].time.isoformat()}",
            strategy="ORB false breakout",
            symbol=symbol, direction=side,
            entry_type="limit",
            entry=entry, sl=sl, tp=tp,
            note=f"False break of ORB {'High' if swept_is_high else 'Low'} - "
                 "limit at overlapping FVG, SL beyond the sweep wick, TP 2R.",
        ))
    return alerts


# ---------------------------------------------------------------------------
# 2. 4H Range False Breakout / CRT Sweep
#    First 4H candle of the NY day = range. 5m close beyond range then the
#    NEXT 5m candle closes back inside -> enter market on that close.
#    CRT: sweep of one side first -> target opposite side of the range.
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

    # first 4H candle of the current NY day
    first_4h = None
    for c in reversed(c4h):
        if c.time.astimezone(NY_TZ).hour in (0, 4, 8, 12, 16, 20):
            first_4h = c
            break
    if first_4h is None:
        return []
    range_high, range_low = first_4h.high, first_4h.low
    post = [c for c in c5 if c.time >= first_4h.time + timedelta(hours=4)]
    if len(post) < 2:
        return []

    tp_mode = _cfg("FOURH_FB_TP_MODE", "opposite")
    alerts: list[Alert] = []
    for direction, beyond, back_inside, swept_extreme, opposite_side in (
        (Direction.BULLISH, lambda c: c.close < range_low, lambda c: c.close > range_low,
         lambda a, b: min(a.low, b.low), range_high),   # swept the LOW -> long, target HIGH
        (Direction.BEARISH, lambda c: c.close > range_high, lambda c: c.close < range_high,
         lambda a, b: max(a.high, b.high), range_low),  # swept the HIGH -> short, target LOW
    ):
        i = 0
        while i < len(post) - 1:
            if beyond(post[i]) and back_inside(post[i + 1]):
                entry = post[i + 1].close
                sl = swept_extreme(post[i], post[i + 1])
                risk = abs(entry - sl)
                if risk <= 0:
                    i += 1
                    continue
                tp = opposite_side if tp_mode == "opposite" else (
                    entry + 2 * risk if direction == Direction.BULLISH else entry - 2 * risk)
                side = "buy" if direction == Direction.BULLISH else "sell"
                alerts.append(Alert(
                    key=f"4h_fb|{symbol}|{side}|{post[i + 1].time.isoformat()}",
                    strategy="4H range false breakout",
                    symbol=symbol, direction=side,
                    entry_type="market",
                    entry=entry, sl=sl, tp=tp,
                    note=f"4H range {range_low:.5g}-{range_high:.5g} swept and reclaimed. "
                         f"TP = {'opposite side (CRT)' if tp_mode == 'opposite' else '2R'}.",
                ))
                i += 2   # multiple trades/day allowed, don't double-count same re-entry
            else:
                i += 1
    return alerts


# ---------------------------------------------------------------------------
# 3. 8AM 1H Range Sweep + IFVG + OB  (PDF strategy 3, generalized key-level
#    tap; the existing "newyork"/"newyork tt" modules are the simplified
#    sibling of this - both stay, this one adds the key-level + OB layer)
# ---------------------------------------------------------------------------

def range_sweep_ifvg_ob_strategy(data: dict) -> list[Alert]:
    alerts: list[Alert] = []
    for symbol in ALL_SYMBOLS:
        alerts += _range_sweep_for_symbol(symbol, data)
    return alerts


def _hourly_key_level_tapped(c1h: list[Candle], sweep_time, swept_low: bool, sweep_price: float) -> bool:
    """Tap of hourly swing low / equal lows / hourly FVG near the sweep."""
    prior = [c for c in c1h if c.time < sweep_time]
    if len(prior) < 3:
        return True   # no structure to contradict; treat as valid
    tol = sweep_price * _cfg("KEYLEVEL_EQUAL_LO_TOLERANCE_PCT", 3e-4)
    swings = find_swings(prior, width=1)
    pts = [s.price for s in swings if s.is_high == (not swept_low)]
    for p in pts:
        if abs(p - sweep_price) <= tol:
            return True
    # equal lows/highs: two structure points at the same price
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            if abs(pts[i] - pts[j]) <= tol:
                return True
    fvgs = find_fvgs(prior)
    zone_dir = Direction.BULLISH if swept_low else Direction.BEARISH
    return any(g.direction == zone_dir and g.bottom <= sweep_price <= g.top for g in fvgs)


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
    for swept_low, level, direction, tp in (
        (True, range_low, Direction.BULLISH, range_high),    # sweep low -> long to range high
        (False, range_high, Direction.BEARISH, range_low),   # sweep high -> short to range low
    ):
        sweep_idx = next(
            (i for i, c in enumerate(working) if (c.low < level if swept_low else c.high > level)),
            None,
        )
        if sweep_idx is None:
            continue
        sweep_price = working[sweep_idx].low if swept_low else working[sweep_idx].high
        if not _hourly_key_level_tapped(c1h, working[sweep_idx].time, swept_low, sweep_price):
            continue
        # 1m close back through a 1m FVG -> IFVG
        fvgs = find_fvgs(working, start_index=max(sweep_idx - 2, 0))
        mark_mitigated(fvgs, working)
        ifvg = detect_ifvg(fvgs, working, from_index=sweep_idx + 1)
        if ifvg is None:
            continue
        ob = find_order_block(working, ifvg.formed_at_index, direction)
        if ob is None or not ob.confirmed:
            continue
        # entry in discount of the manipulation leg: OB / IFVG midpoint zone
        entry = min(ob.midpoint if hasattr(ob, "midpoint") else ob.price,
                    working[ifvg.formed_at_index].close)
        sl = sweep_price
        side = "buy" if direction == Direction.BULLISH else "sell"
        alerts.append(Alert(
            key=f"range_sweep|{symbol}|{side}|{working[ifvg.formed_at_index].time.isoformat()}",
            strategy="Range sweep IFVG OB",
            symbol=symbol, direction=side,
            entry_type="limit",
            entry=entry, sl=sl, tp=tp,
            note=f"8AM range {range_low:.5g}-{range_high:.5g} "
                 f"{'low' if swept_low else 'high'} swept at key level; "
                 "entry in discount of the sweep, SL at sweep extreme, TP opposite side of range.",
        ))
    return alerts


# ---------------------------------------------------------------------------
# 4. Daily Profile Session Bias  (FILTER - not an entry signal)
#    Asia 8PM-12AM / London 2AM-5AM / NY after 8AM (all NY time).
# ---------------------------------------------------------------------------

def daily_session_bias(data: dict, symbol: str = "EURUSD") -> str:
    c5 = data.get((symbol, "5M"), [])
    if not c5:
        return "neutral"

    def window(h1, h2):
        return [c for c in c5
                if h1 <= (t := c.time.astimezone(NY_TZ)).hour < h2
                or (h2 == 0 and t.hour >= h1)]

    asia = window(20, 0)
    london = window(2, 5)
    if not asia or not london:
        return "neutral"
    asia_high, asia_low = max(c.high for c in asia), min(c.low for c in asia)

    london_swept_asia_high = any(c.high > asia_high for c in london)
    london_swept_asia_low = any(c.low < asia_low for c in london)
    distributed_down = any(c.close < asia_low for c in london)      # push lower after sweep
    distributed_up = any(c.close > asia_high for c in london)

    # London swept Asia High but did NOT distribute down -> expect NY to push up
    if london_swept_asia_high and not distributed_down:
        return "bullish"
    # London swept Asia High AND pushed lower -> NY continues down
    if london_swept_asia_high and distributed_down:
        return "bearish"
    # London swept Asia Low but did NOT distribute up -> expect NY to push down
    if london_swept_asia_low and not distributed_up:
        return "bearish"
    if london_swept_asia_low and distributed_up:
        return "bullish"
    # No sweep at all -> expect NY to sweep London High/Low, then distribute
    london_high, london_low = max(c.high for c in london), min(c.low for c in london)
    post_london = [c for c in c5 if c.time > london[-1].time]
    if post_london and any(c.high > london_high for c in post_london):
        return "bearish"   # swept London high -> look for distribution down
    if post_london and any(c.low < london_low for c in post_london):
        return "bullish"
    return "neutral"


# ---------------------------------------------------------------------------
# 5. SMT Divergence Confirmation  (confirmation only, not an entry by itself)
#    Bullish: anchor makes lower low, comparison makes higher low, then the
#    anchor prints MSS + FVG/OB in the reversal direction.
# ---------------------------------------------------------------------------

def smt_divergence_strategy(data: dict, anchor: str = "NAS100",
                            comparison: str = "EURUSD") -> list[Alert]:
    a5 = data.get((anchor, "5M"), [])
    c5 = data.get((comparison, "5M"), [])
    if len(a5) < 10 or len(c5) < 10:
        return []

    def last_swing_lows(candles):
        swings = find_swings(candles, width=_cfg("SMT_SWING_WIDTH", 1))
        return [s for s in swings if not s.is_high][-2:]

    a_lows, c_lows = last_swing_lows(a5), last_swing_lows(c5)
    if len(a_lows) < 2 or len(c_lows) < 2:
        return []
    bullish_smt = a_lows[-1].price < a_lows[-2].price and c_lows[-1].price > c_lows[-2].price
    bearish_smt = (a_lows[-1].price > a_lows[-2].price and c_lows[-1].price < c_lows[-2].price)

    direction = Direction.BULLISH if bullish_smt else (Direction.BEARISH if bearish_smt else None)
    if direction is None:
        return []
    if (utc_now() - a_lows[-1].time).total_seconds() > 6 * 3600:
        return []   # stale divergence

    # After SMT wait for: MSS (close beyond causal swing) + FVG/OB + displacement
    swings = find_swings(a5, width=1)
    ref = last_swing_before(swings, len(a5) - 1, is_high=(direction == Direction.BEARISH))
    if ref is None:
        return []
    mss_idx = _close_beyond(a5, ref.price, ref.index + 1, direction)
    if mss_idx is None:
        return []
    fvgs = find_fvgs(a5, start_index=max(mss_idx - 2, 0))
    mark_mitigated(fvgs, a5)
    gap = unmitigated_fvg_in_range(fvgs, a5, mss_idx, len(a5) - 1, direction)
    ob = find_order_block(a5, mss_idx, direction)
    if gap is None and (ob is None or not ob.confirmed):
        return []

    entry = gap.midpoint if gap is not None else ob.price
    sl = ref.price
    risk = abs(entry - sl)
    tp = entry + 2 * risk if direction == Direction.BULLISH else entry - 2 * risk
    side = "buy" if direction == Direction.BULLISH else "sell"
    return [Alert(
        key=f"smt|{anchor}|{side}|{a5[mss_idx].time.isoformat()}",
        strategy="SMT divergence",
        symbol=anchor, direction=side,
        entry_type="limit",
        entry=entry, sl=sl, tp=tp,
        note=f"{'Bullish' if bullish_smt else 'Bearish'} SMT vs {comparison} + MSS confirmed. "
             "Confirmation module - check HTF bias before taking it.",
    )]


# ---------------------------------------------------------------------------
# 6. Liquidity Pool Targets  (helper - ranked TP / draw-on-liquidity levels)
#    Priority: unfilled 5m/15m FVG > session highs/lows > 15m swings inside
#    FVG > relative equal lows/highs.
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
        now = utc_now()
        for name, (h1, h2) in (("Asia", (20, 0)), ("London", (2, 5))):
            sess = [c for c in c5 if h1 <= (t := c.time.astimezone(NY_TZ)).hour < h2 or (h2 == 0 and t.hour >= h1)]
            if sess:
                lvl = min(c.low for c in sess) if direction == Direction.BULLISH else max(c.high for c in sess)
                pools.append((lvl, f"{name} session {'low' if direction == Direction.BULLISH else 'high'}"))
    # de-dup, keep priority order
    seen, ranked = set(), []
    for price, label in pools:
        key = round(price, 6)
        if key not in seen:
            seen.add(key)
            ranked.append((price, label))
    return ranked


# ---------------------------------------------------------------------------
# 7. Breakout + Momentum + Chandelier Exit
#    Consolidation (>= 2 touches of S/R) -> momentum candle (big body or 3
#    consecutive same-direction candles) closes beyond resistance -> enter
#    at close. TP1 at 1.5R (sell half, move SL to old TP), trail the rest
#    with a Chandelier Stop (ATR period 14, multiplier 2).
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
    body_pct_min = _cfg("MOMENTUM_BODY_PCT", 0.60)
    atr = _atr(c5, _cfg("CHANDELIER_ATR_PERIOD", 14))
    if atr is None:
        return []

    alerts: list[Alert] = []
    swings = find_swings(c5, width=2)
    highs = [s for s in swings if s.is_high]
    lows = [s for s in swings if not s.is_high]
    tol = c5[-1].close * _cfg("TOUCH_TOLERANCE_PCT", 2e-4)

    for direction, level_swings, beyond in (
        (Direction.BULLISH, highs, lambda c, lvl: c.close > lvl),
        (Direction.BEARISH, lows, lambda c, lvl: c.close < lvl),
    ):
        if len(level_swings) < 2:
            continue
        # resistance = cluster of >=2 swing highs at ~the same price
        level = None
        for i in range(len(level_swings) - 1):
            for j in range(i + 1, len(level_swings)):
                if abs(level_swings[i].price - level_swings[j].price) <= tol:
                    level = max(level_swings[i].price, level_swings[j].price)
                    break
            if level is not None:
                break
        if level is None:
            continue
        after = [i for i, c in enumerate(c5) if c.time > level_swings[-1].time]
        for i in after:
            c = c5[i]
            if not beyond(c, level):
                continue
            big_body = c.body_pct_of_range >= body_pct_min and (
                c.is_bullish if direction == Direction.BULLISH else c.is_bearish)
            three_in_row = i >= 2 and all(
                (c5[k].is_bullish if direction == Direction.BULLISH else c5[k].is_bearish)
                for k in (i - 2, i - 1, i))
            if not (big_body or three_in_row):
                continue
            entry = c.close
            sl = (min(s.price for s in lows[-3:]) if direction == Direction.BULLISH
                  else max(s.price for s in highs[-3:]))
            if (direction == Direction.BULLISH and sl >= entry) or (direction == Direction.BEARISH and sl <= entry):
                sl = level
            risk = abs(entry - sl)
            tp1 = entry + _cfg("MOMENTUM_TP1_R", 1.5) * risk if direction == Direction.BULLISH \
                else entry - _cfg("MOMENTUM_TP1_R", 1.5) * risk
            chandelier = (c.high - _cfg("CHANDELIER_ATR_MULT", 2.0) * atr) if direction == Direction.BULLISH \
                else (c.low + _cfg("CHANDELIER_ATR_MULT", 2.0) * atr)
            side = "buy" if direction == Direction.BULLISH else "sell"
            alerts.append(Alert(
                key=f"breakout_mom|{symbol}|{side}|{c.time.isoformat()}",
                strategy="Breakout momentum",
                symbol=symbol, direction=side,
                entry_type="market",
                entry=entry, sl=sl, tp=tp1,
                note=f"Consolidation breakout at {level:.5g} with momentum. "
                     f"TP1 = 1.5R (sell half, move SL to {tp1:.5g}); trail remainder with "
                     f"Chandelier stop ~{chandelier:.5g} (ATR14 x2). Exit on colour flip.",
            ))
            break   # one alert per direction per run
    return alerts


# ---------------------------------------------------------------------------
# 8. Top-Down MTF Framework  (FILTER for all strategies)
#    HTF 1H trend -> MTF 15M structure aligned -> LTF 5M entry zone.
#    Returns Direction or None; use to veto counter-trend entries.
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
    """Apply the Top-Down filter to any alert list (spec: combine modules
    with filters from #4, #5, #6, #8). Neutral-bias symbols pass through
    only when the trade agrees with the HTF 1H structure."""
    kept = []
    for a in alerts:
        bias = topdown_bias(data, a.symbol)
        if bias is None:
            if daily_session_bias(data, a.symbol) == (
                    "buy" if a.direction == "buy" else "sell"):
                kept.append(a)
            continue
        if (bias == Direction.BULLISH and a.direction == "buy") or \
           (bias == Direction.BEARISH and a.direction == "sell"):
            kept.append(a)
    return kept


ALL_SMC_STRATEGIES = [
    orb_breakout_strategy,
    fourh_range_false_breakout_strategy,
    range_sweep_ifvg_ob_strategy,
    smt_divergence_strategy,
    breakout_momentum_strategy,
]
