"""
Reusable filter / confirmation helpers for the ChartTactix and session-bias
concepts. These are exposed for querying (Telegram / dashboard) and can be
called by strategies, but do not automatically gate alerts unless a strategy
explicitly imports and uses them.

Contents:
  - daily_profile_session_bias: Asian + London range interaction -> NY bias
  - smt_divergence_confirmation: thin wrapper around detect_smt_divergence
  - liquidity_pool_targets: ranked TP suggestions (unfilled FVG, session extremes, equal highs/lows)
  - top_down_mtf_framework: simplified Daily/4H/1H alignment (Weekly/Monthly
    not fetched by the bot's data plan)
"""

from dataclasses import dataclass
from typing import Optional

from src.candles import (
    Candle, Direction, find_fvgs, mark_mitigated, find_swings, last_swing_before,
    detect_smt_divergence, structure_trend, utc_now,
)
from src.killzones import ASIAN, LONDON, NY_TZ
from src.strategies import ALL_SYMBOLS, CORRELATED_PAIR


@dataclass
class SessionBias:
    bias: str  # "bullish" | "bearish" | "neutral"
    note: str


def daily_profile_session_bias(data: dict, symbol: str) -> Optional[SessionBias]:
    """Using Asian (20:00-00:00 NY) and London (02:00-05:00 NY) sessions,
    determine bias for the New York session.

    Rules (mirrored for highs/lows):
    - If London sweeps the Asian high and then shows no clear bearish
      structure afterward -> bias = bearish (NY likely completes the move).
    - If London already shows bearish structure after the sweep -> bias = bearish
      (continuation).
    - Symmetric for the low / bullish case.
    - If London sweeps neither extreme -> bias = neutral
      ("expect NY to sweep London's range, then reverse").
    """
    c5 = data.get((symbol, "5M"), []) or data.get((symbol, "15M"), [])
    if not c5 or len(c5) < 20:
        return None

    now = utc_now()
    asian_start, asian_end = ASIAN.current_or_most_recent_window(now)
    london_start, london_end = LONDON.current_or_most_recent_window(now)

    asian_candles = [c for c in c5 if asian_start <= c.time < asian_end]
    london_candles = [c for c in c5 if london_start <= c.time < london_end]
    if not asian_candles or not london_candles:
        return None

    asian_high = max(c.high for c in asian_candles)
    asian_low = min(c.low for c in asian_candles)

    london_swept_high = any(c.high > asian_high for c in london_candles)
    london_swept_low = any(c.low < asian_low for c in london_candles)

    if not london_swept_high and not london_swept_low:
        return SessionBias(
            bias="neutral",
            note="London swept neither Asian extreme — expect NY to sweep London's range, then reverse.",
        )

    post_sweep = london_candles
    trend = structure_trend(post_sweep, width=1)

    if london_swept_high:
        # Bearish bias expected
        if trend == Direction.BEARISH:
            return SessionBias(
                bias="bearish",
                note="London swept Asian high and already shows bearish structure — continuation bias.",
            )
        return SessionBias(
            bias="bearish",
            note="London swept Asian high without clear bearish structure yet — NY likely completes the move down.",
        )

    if london_swept_low:
        if trend == Direction.BULLISH:
            return SessionBias(
                bias="bullish",
                note="London swept Asian low and already shows bullish structure — continuation bias.",
            )
        return SessionBias(
            bias="bullish",
            note="London swept Asian low without clear bullish structure yet — NY likely completes the move up.",
        )

    return SessionBias(bias="neutral", note="No clear session bias.")


def smt_divergence_confirmation(data: dict, symbol: str, lookback: int = 30) -> Optional[Direction]:
    """Thin wrapper around detect_smt_divergence. Returns the divergence
    direction or None if data is insufficient / no divergence. Never fabricates."""
    correlated = CORRELATED_PAIR.get(symbol)
    if not correlated:
        return None
    primary = data.get((symbol, "15M"), [])
    corr = data.get((correlated, "15M"), [])
    if not primary or not corr:
        return None
    return detect_smt_divergence(primary, corr, lookback=lookback)


@dataclass
class LiquidityTarget:
    kind: str  # "fvg" | "session_extreme" | "equal_highs_lows"
    price: float
    note: str


def liquidity_pool_targets(data: dict, symbol: str, direction: str) -> list[LiquidityTarget]:
    """Ranked suggested TP levels for the given direction:
    1. Nearest unfilled FVG in that direction
    2. Asian / London session extreme
    3. Nearest equal-highs / equal-lows cluster
    """
    targets: list[LiquidityTarget] = []
    c15 = data.get((symbol, "15M"), [])
    c5 = data.get((symbol, "5M"), []) or c15
    if not c15:
        return targets

    dir_enum = Direction.BULLISH if direction == "buy" else Direction.BEARISH

    # 1. Unfilled FVG
    fvgs = find_fvgs(c15)
    mark_mitigated(fvgs, c15)
    unfilled = [g for g in fvgs if g.direction == dir_enum and not g.mitigated]
    if unfilled:
        nearest = unfilled[-1]
        targets.append(LiquidityTarget(
            kind="fvg",
            price=nearest.midpoint,
            note=f"Nearest unmitigated {direction} FVG midpoint",
        ))

    # 2. Session extremes
    now = utc_now()
    asian_start, asian_end = ASIAN.current_or_most_recent_window(now)
    london_start, london_end = LONDON.current_or_most_recent_window(now)
    asian = [c for c in c5 if asian_start <= c.time < asian_end]
    london = [c for c in c5 if london_start <= c.time < london_end]
    if asian:
        if direction == "buy":
            targets.append(LiquidityTarget(
                kind="session_extreme",
                price=max(c.high for c in asian),
                note="Asian session high",
            ))
        else:
            targets.append(LiquidityTarget(
                kind="session_extreme",
                price=min(c.low for c in asian),
                note="Asian session low",
            ))
    if london:
        if direction == "buy":
            targets.append(LiquidityTarget(
                kind="session_extreme",
                price=max(c.high for c in london),
                note="London session high",
            ))
        else:
            targets.append(LiquidityTarget(
                kind="session_extreme",
                price=min(c.low for c in london),
                note="London session low",
            ))

    # 3. Equal highs / lows (simple: last two swings within 0.1% of each other)
    swings = find_swings(c15, width=1)
    if direction == "buy":
        highs = [s for s in swings if s.is_high]
        if len(highs) >= 2:
            a, b = highs[-1], highs[-2]
            if abs(a.price - b.price) / max(a.price, 1e-9) < 0.001:
                targets.append(LiquidityTarget(
                    kind="equal_highs_lows",
                    price=(a.price + b.price) / 2,
                    note="Nearest equal-highs cluster",
                ))
    else:
        lows = [s for s in swings if not s.is_high]
        if len(lows) >= 2:
            a, b = lows[-1], lows[-2]
            if abs(a.price - b.price) / max(a.price, 1e-9) < 0.001:
                targets.append(LiquidityTarget(
                    kind="equal_highs_lows",
                    price=(a.price + b.price) / 2,
                    note="Nearest equal-lows cluster",
                ))

    return targets


@dataclass
class MTFAlignment:
    aligned: bool
    direction: Optional[str]  # "buy" | "sell" | None
    details: dict  # period -> "bullish"|"bearish"|"mixed"|None
    note: str


def top_down_mtf_framework(data: dict, symbol: str) -> Optional[MTFAlignment]:
    """Simplified top-down alignment using Daily / 4H / 1H (Weekly and Monthly
    candle data are not currently fetched by the bot's data plan).

    Returns alignment status and the consensus direction when all three agree.
    """
    periods = ("1D", "4H", "1H")
    details = {}
    dirs = []
    for p in periods:
        candles = data.get((symbol, p), [])
        if len(candles) < 10:
            details[p] = None
            continue
        trend = structure_trend(candles, width=1)
        if trend == Direction.BULLISH:
            details[p] = "bullish"
            dirs.append("buy")
        elif trend == Direction.BEARISH:
            details[p] = "bearish"
            dirs.append("sell")
        else:
            details[p] = "mixed"

    if not dirs:
        return MTFAlignment(
            aligned=False,
            direction=None,
            details=details,
            note="Insufficient higher-timeframe data for alignment.",
        )

    if len(set(dirs)) == 1 and len(dirs) == 3:
        return MTFAlignment(
            aligned=True,
            direction=dirs[0],
            details=details,
            note=f"Daily/4H/1H all {details['1D']} — aligned {dirs[0].upper()}.",
        )

    return MTFAlignment(
        aligned=False,
        direction=None,
        details=details,
        note="Higher timeframes are mixed or incomplete — no clear top-down bias.",
    )
