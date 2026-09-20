"""
Candle-level detection primitives shared by every strategy.

UPDATED: added three new primitives for the ChartTactix strategy set
(strategies 6-10 in src/strategies.py):
  - find_bpr_zones: Balanced Price Range (overlapping opposite-direction
    FVG pair)
  - find_all_order_blocks / find_breaker_blocks: scans a whole series
    for every order block, then finds which ones later got invalidated
    and flipped role (a "breaker block")
  - detect_smt_divergence: Smart Money Divergence against a correlated
    instrument - returns None (never fabricates) whenever either series
    lacks enough data, per project rule
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class Direction(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"


@dataclass
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float

    @property
    def is_bullish(self):
        return self.close > self.open

    @property
    def is_bearish(self):
        return self.close < self.open

    @property
    def body_pct_of_range(self):
        rng = self.high - self.low
        if rng == 0:
            return 0
        return abs(self.close - self.open) / rng


@dataclass
class Swing:
    index: int
    price: float
    is_high: bool


def find_swings(candles, width=1):
    swings = []
    for i in range(width, len(candles) - width):
        c = candles[i]
        left = candles[i-width:i]
        right = candles[i+1:i+1+width]
        if all(c.high > o.high for o in left+right):
            swings.append(Swing(i, c.high, True))
        if all(c.low < o.low for o in left+right):
            swings.append(Swing(i, c.low, False))
    return swings


def last_swing_before(swings, index, is_high):
    candidates = [s for s in swings if s.index < index and s.is_high == is_high]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.index)


@dataclass
class FVG:
    formed_at_index: int
    top: float
    bottom: float
    direction: Direction
    mitigated: bool = False

    @property
    def midpoint(self):
        return (self.top + self.bottom) / 2


def find_fvgs(candles, start_index=0):
    fvgs = []
    for i in range(max(start_index, 2), len(candles)):
        a, b, c = candles[i-2], candles[i-1], candles[i]
        if a.high < c.low:
            fvgs.append(FVG(i, c.low, a.high, Direction.BULLISH))
        if a.low > c.high:
            fvgs.append(FVG(i, a.low, c.high, Direction.BEARISH))
    return fvgs


def mark_mitigated(fvgs, candles):
    for g in fvgs:
        for c in candles[g.formed_at_index+1:]:
            if c.low <= g.top and c.high >= g.bottom:
                g.mitigated = True
                break


def unmitigated_fvg_in_range(fvgs, candles, start_idx, end_idx, direction):
    candidates = [g for g in fvgs if g.direction == direction and not g.mitigated
                  and start_idx <= g.formed_at_index <= end_idx]
    if not candidates:
        return None
    return candidates[-1]


@dataclass
class OrderBlock:
    index: int
    confirmed: bool
    top: float = None
    bottom: float = None
    direction: Direction = None


def find_order_block(candles, from_index, direction):
    """Original single-lookup version - scans BACKWARD from from_index
    for the most recent opposite-colour candle before a break. Used by
    the 9AM CR model (newyork strategies) - unchanged."""
    opposite_bullish = direction == Direction.BEARISH
    for i in range(from_index - 1, -1, -1):
        c = candles[i]
        if (c.is_bullish if opposite_bullish else c.is_bearish):
            confirmed = False
            if i+1 < len(candles):
                nxt = candles[i+1]
                if direction == Direction.BULLISH and nxt.close > c.high:
                    confirmed = True
                if direction == Direction.BEARISH and nxt.close < c.low:
                    confirmed = True
            return OrderBlock(i, confirmed, top=max(c.open, c.close), bottom=min(c.open, c.close), direction=direction)
    return None


def find_all_order_blocks(candles):
    """NEW - scans the WHOLE series forward for every order block (last
    opposite-colour candle before a displacement move >=1.5x average
    body). Used by the new Breaker Block strategies, which need to
    check every past order block for later invalidation, not just the
    one nearest a specific index."""
    blocks = []
    body_sizes = [abs(c.close - c.open) for c in candles]
    avg_body = sum(body_sizes) / len(body_sizes) if body_sizes else 0
    for i in range(2, len(candles)):
        displacement = candles[i]
        disp_body = abs(displacement.close - displacement.open)
        if disp_body < avg_body * 1.5:
            continue
        prev = candles[i - 1]
        is_bull_disp = displacement.is_bullish
        prev_is_opposite = prev.is_bearish if is_bull_disp else prev.is_bullish
        if not prev_is_opposite:
            continue
        blocks.append(OrderBlock(
            index=i - 1, confirmed=True,
            top=max(prev.open, prev.close), bottom=min(prev.open, prev.close),
            direction=Direction.BULLISH if is_bull_disp else Direction.BEARISH,
        ))
    return blocks


def detect_ifvg(fvgs, candles, from_index=0):
    for g in fvgs:
        for c in candles[g.formed_at_index+1:]:
            if g.direction == Direction.BULLISH and c.close < g.bottom:
                return g
            if g.direction == Direction.BEARISH and c.close > g.top:
                return g
    return None


@dataclass
class BPR:
    top: float
    bottom: float
    direction: Direction
    newer_fvg_index: int
    older_fvg_index: int

    @property
    def midpoint(self):
        return (self.top + self.bottom) / 2


def find_bpr_zones(fvgs):
    """fvgs should be the RAW (not mitigation-filtered) list - a BPR is
    the overlap of any bullish/bearish FVG pair regardless of whether
    either side has since been tapped. Traded in the direction of
    whichever FVG formed most recently."""
    zones = []
    bullish = [g for g in fvgs if g.direction == Direction.BULLISH]
    bearish = [g for g in fvgs if g.direction == Direction.BEARISH]
    for b in bullish:
        for r in bearish:
            top = min(b.top, r.top)
            bottom = max(b.bottom, r.bottom)
            if top <= bottom:
                continue
            newer, older = (b, r) if b.formed_at_index > r.formed_at_index else (r, b)
            zones.append(BPR(
                top=top, bottom=bottom, direction=newer.direction,
                newer_fvg_index=newer.formed_at_index, older_fvg_index=older.formed_at_index,
            ))
    return zones


@dataclass
class BreakerBlock:
    top: float
    bottom: float
    direction: Direction          # the NEW role after flipping
    invalidated_at_index: int
    original_ob_index: int


def find_breaker_blocks(candles, order_blocks):
    """A bullish OB becomes a bearish breaker if price later CLOSES
    below its bottom (support failed -> flips to resistance). Symmetric
    for a bearish OB closing above its top."""
    breakers = []
    for ob in order_blocks:
        for j in range(ob.index + 1, len(candles)):
            c = candles[j]
            if ob.direction == Direction.BULLISH and c.close < ob.bottom:
                breakers.append(BreakerBlock(
                    top=ob.top, bottom=ob.bottom, direction=Direction.BEARISH,
                    invalidated_at_index=j, original_ob_index=ob.index,
                ))
                break
            if ob.direction == Direction.BEARISH and c.close > ob.top:
                breakers.append(BreakerBlock(
                    top=ob.top, bottom=ob.bottom, direction=Direction.BULLISH,
                    invalidated_at_index=j, original_ob_index=ob.index,
                ))
                break
    return breakers


def detect_smt_divergence(primary_candles, correlated_candles, lookback=30):
    """Returns Direction.BEARISH if the primary instrument made a new
    high the correlated one didn't (liquidity grab, likely to reverse
    down), Direction.BULLISH for the symmetric new-low case, or None if
    there's no divergence OR either series lacks enough data. NEVER
    fabricates a signal when data is insufficient."""
    if len(primary_candles) < lookback or len(correlated_candles) < lookback:
        return None

    p = primary_candles[-lookback:]
    c = correlated_candles[-lookback:]

    p_prior_high = max(x.high for x in p[:-1])
    c_prior_high = max(x.high for x in c[:-1])
    if p[-1].high > p_prior_high and not (c[-1].high > c_prior_high):
        return Direction.BEARISH

    p_prior_low = min(x.low for x in p[:-1])
    c_prior_low = min(x.low for x in c[:-1])
    if p[-1].low < p_prior_low and not (c[-1].low < c_prior_low):
        return Direction.BULLISH

    return None


def structure_trend(candles, width=1):
    swings = find_swings(candles, width=width)
    highs = [s for s in swings if s.is_high]
    lows = [s for s in swings if not s.is_high]
    if len(highs) < 2 or len(lows) < 2:
        return None
    if highs[-1].price > highs[-2].price and lows[-1].price > lows[-2].price:
        return Direction.BULLISH
    if highs[-1].price < highs[-2].price and lows[-1].price < lows[-2].price:
        return Direction.BEARISH
    return None


def raw_trendbar_to_candle(open_time, low, delta_open, delta_high, delta_close):
    return Candle(
        time=open_time,
        open=low + delta_open,
        high=low + delta_high,
        low=low,
        close=low + delta_close,
    )


def utc_now():
    return datetime.now(timezone.utc)
