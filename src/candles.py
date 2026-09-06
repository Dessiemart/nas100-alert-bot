"""
Shared candle data structure and market-structure detection helpers used
by every strategy: swing points (fractals), fair value gaps (FVG),
inverse FVGs (IFVG), order blocks (OB), and break/change of structure
(BOS/CHoCH).

These are implemented exactly to the rules we agreed on while defining
each strategy - see /areas/nasdaq-alert-system.md for the plain-English
version of each rule. Where a rule said "no extra filter", this code
adds none; where it said "candle close required", this code checks the
close, not the wick.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


@dataclass
class Candle:
    time: datetime  # open time, UTC
    open: float
    high: float
    low: float
    close: float

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)

    @property
    def range_size(self) -> float:
        return self.high - self.low

    @property
    def body_pct_of_range(self) -> float:
        if self.range_size == 0:
            return 0.0
        return self.body_size / self.range_size


class Direction(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"


@dataclass
class FVG:
    direction: Direction
    top: float      # upper boundary of the gap
    bottom: float   # lower boundary of the gap
    formed_at_index: int  # index of the middle candle (candle 2 of the 3)
    mitigated: bool = False

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2.0


@dataclass
class OrderBlock:
    direction: Direction  # direction of the displacement that followed it
    candle: Candle
    index: int
    confirmed: bool = False


@dataclass
class SwingPoint:
    index: int
    price: float
    is_high: bool


def raw_trendbar_to_candle(open_time_utc: datetime, low_raw: int, delta_open: int,
                            delta_high: int, delta_close: int) -> Candle:
    """Convert a cTrader ProtoOATrendbar (relative integer format) into a
    real-valued Candle. Per cTrader's docs: divide the low by 100000, then
    add each delta (also /100000) to get open/high/close."""
    low = low_raw / 100000.0
    return Candle(
        time=open_time_utc,
        open=low + delta_open / 100000.0,
        high=low + delta_high / 100000.0,
        low=low,
        close=low + delta_close / 100000.0,
    )


def find_swings(candles: list[Candle], width: int = 1) -> list[SwingPoint]:
    """A candle at index i is a swing high if its high is strictly greater
    than the highs of `width` candles on each side (and mirrored for
    swing lows). This is the basic fractal rule used throughout."""
    swings: list[SwingPoint] = []
    n = len(candles)
    for i in range(width, n - width):
        window = candles[i - width:i + width + 1]
        center = candles[i]
        if all(center.high > c.high for c in window if c is not center):
            swings.append(SwingPoint(index=i, price=center.high, is_high=True))
        if all(center.low < c.low for c in window if c is not center):
            swings.append(SwingPoint(index=i, price=center.low, is_high=False))
    return swings


def last_swing_before(swings: list[SwingPoint], index: int, is_high: Optional[bool] = None) -> Optional[SwingPoint]:
    candidates = [s for s in swings if s.index < index and (is_high is None or s.is_high == is_high)]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.index)


def find_fvgs(candles: list[Candle], start_index: int = 0) -> list[FVG]:
    """Classic 3-candle FVG: candle1.high < candle3.low (bullish gap) or
    candle1.low > candle3.high (bearish gap). Gap boundaries are
    candle1.high/candle3.low (bullish) or candle3.high/candle1.low
    (bearish)."""
    fvgs: list[FVG] = []
    for i in range(max(start_index, 0) + 2, len(candles)):
        c1, c3 = candles[i - 2], candles[i]
        if c1.high < c3.low:
            fvgs.append(FVG(direction=Direction.BULLISH, top=c3.low, bottom=c1.high, formed_at_index=i - 1))
        elif c1.low > c3.high:
            fvgs.append(FVG(direction=Direction.BEARISH, top=c1.low, bottom=c3.high, formed_at_index=i - 1))
    return fvgs


def mark_mitigated(fvgs: list[FVG], candles: list[Candle], touch_only: bool = True) -> None:
    """Mark each FVG mitigated if price has wicked into the zone and
    (for touch_only=True) that's sufficient - matches the 'Asian tt'
    definition where a touch is enough, no full close-through needed."""
    for gap in fvgs:
        for c in candles[gap.formed_at_index + 1:]:
            touched = c.low <= gap.top and c.high >= gap.bottom
            if touched:
                gap.mitigated = True
                break


def unmitigated_fvg_in_range(fvgs: list[FVG], candles: list[Candle], leg_start: int, leg_end: int,
                              direction: Direction) -> Optional[FVG]:
    """Return the (first) unmitigated FVG of the given direction formed
    within [leg_start, leg_end]."""
    for gap in fvgs:
        if gap.direction != direction:
            continue
        if not (leg_start <= gap.formed_at_index <= leg_end):
            continue
        if not gap.mitigated:
            return gap
    return None


def find_order_block(candles: list[Candle], displacement_index: int, direction: Direction) -> Optional[OrderBlock]:
    """The last opposite-color candle before a displacement move,
    confirmed once the *next* candle closes beyond it (per the 'newyork'
    strategy definition). `displacement_index` is the index of the
    displacement candle itself."""
    opposite_is_bearish = direction == Direction.BULLISH  # bullish displacement -> look for last bearish candle
    for i in range(displacement_index - 1, -1, -1):
        c = candles[i]
        if opposite_is_bearish and c.is_bearish:
            ob_candle = c
            ob_index = i
            break
        if not opposite_is_bearish and c.is_bullish:
            ob_candle = c
            ob_index = i
            break
    else:
        return None

    confirmed = False
    if displacement_index + 1 < len(candles):
        confirm_candle = candles[displacement_index + 1]
        if direction == Direction.BULLISH:
            confirmed = confirm_candle.close > ob_candle.high
        else:
            confirmed = confirm_candle.close < ob_candle.low
    return OrderBlock(direction=direction, candle=ob_candle, index=ob_index, confirmed=confirmed)


def detect_ifvg(fvgs: list[FVG], candles: list[Candle], from_index: int) -> Optional[FVG]:
    """An IFVG forms when an existing (unmitigated-until-now) FVG has its
    FAR edge fully closed through by an opposite-direction candle,
    flipping its role. Returns a new FVG object with direction flipped,
    anchored at the candle that broke it."""
    for i in range(from_index, len(candles)):
        c = candles[i]
        for gap in fvgs:
            if gap.formed_at_index >= i:
                continue
            if gap.direction == Direction.BULLISH and c.close < gap.bottom:
                # bullish FVG invalidated downward -> bearish IFVG
                gap.mitigated = True
                return FVG(direction=Direction.BEARISH, top=gap.top, bottom=gap.bottom,
                           formed_at_index=i, mitigated=False)
            if gap.direction == Direction.BEARISH and c.close > gap.top:
                # bearish FVG invalidated upward -> bullish IFVG
                gap.mitigated = True
                return FVG(direction=Direction.BULLISH, top=gap.top, bottom=gap.bottom,
                           formed_at_index=i, mitigated=False)
    return None


def structure_trend(candles: list[Candle], width: int = 1) -> Optional[Direction]:
    """Visual HH/HL vs LH/LL trend read, using the swing sequence. Returns
    None ('UNCLEAR') if the last two highs/lows don't agree on a single
    direction."""
    swings = find_swings(candles, width=width)
    highs = [s for s in swings if s.is_high]
    lows = [s for s in swings if not s.is_high]
    if len(highs) < 2 or len(lows) < 2:
        return None
    hh = highs[-1].price > highs[-2].price
    hl = lows[-1].price > lows[-2].price
    lh = highs[-1].price < highs[-2].price
    ll = lows[-1].price < lows[-2].price
    if hh and hl:
        return Direction.BULLISH
    if lh and ll:
        return Direction.BEARISH
    return None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
