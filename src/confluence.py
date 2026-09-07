"""
Partial-confluence progress tracking.

For each strategy, this breaks the entry conditions into an ordered
checklist and reports progress even when the full setup hasn't confirmed
yet - so you get a heads-up like "4/5 confluences met, waiting on the
reaction candle" instead of only hearing about it once it's already a
complete, tradeable signal.

This intentionally re-derives each strategy's logic step by step rather
than reusing the strategy functions directly, because those functions
stop (return None) at the first failed check - exactly what we don't
want here, since the whole point is seeing how far a setup has gotten.
Some approximation is used for the more branchy rules (e.g. 'London tt'
weak-candle exception) - full precision is reserved for the real alert
in src/strategies.py; this is a heads-up, not the trigger itself.
"""

from dataclasses import dataclass, field

import config
from src.candles import (
    Direction, find_swings, last_swing_before, find_fvgs, mark_mitigated,
    unmitigated_fvg_in_range, find_order_block, detect_ifvg, structure_trend, utc_now,
)
from src.killzones import ASIAN, NEW_YORK_AM, NY_TZ, pre_london_window
from src.strategies import _close_beyond, _zone_tap, _reaction_candle


@dataclass
class ConfluenceCheck:
    strategy: str
    symbol: str
    direction: str  # "buy", "sell", or "unknown"
    step_names: list
    step_results: list  # same length as step_names, in order

    @property
    def total(self) -> int:
        return len(self.step_names)

    @property
    def confirmed(self) -> int:
        return sum(1 for ok in self.step_results if ok)

    @property
    def key(self) -> str:
        return f"progress|{self.strategy}|{self.symbol}|{self.direction}|{self.confirmed}of{self.total}"

    def is_near_complete(self) -> bool:
        missing = self.total - self.confirmed
        return 0 < missing <= config.NEAR_MISS_MAX_MISSING

    def format_message(self) -> str:
        lines = [f"🟡 <b>{self.confirmed}/{self.total} confluences</b> — {self.symbol} ({self.strategy})"]
        if self.direction != "unknown":
            lines.append(f"Leaning: {'BUY' if self.direction == 'buy' else 'SELL'}")
        for name, ok in zip(self.step_names, self.step_results):
            lines.append(f"{'✅' if ok else '⏳'} {name}")
        lines.append("⚠️ Not a full setup yet — this is a heads-up, not an entry signal.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 1. "Asians"
# ---------------------------------------------------------------------------

def check_asians_progress(data: dict) -> list[ConfluenceCheck]:
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
    results: list[ConfluenceCheck] = []

    for swept_is_high in (True, False):
        level = asian_high if swept_is_high else asian_low
        sweep_idx = next(
            (i for i, c in enumerate(post_asian) if (c.high > level if swept_is_high else c.low < level)),
            None,
        )
        swept = sweep_idx is not None

        if trend is None:
            if swept:
                results.append(ConfluenceCheck(
                    "Asians", symbol, "unknown",
                    ["Asian high/low swept", "1H trend clear (continuation vs reversal)"],
                    [True, False],
                ))
            continue

        uptrend = trend == Direction.BULLISH
        if swept_is_high:
            mode, direction = ("continuation", Direction.BULLISH) if uptrend else ("reversal", Direction.BEARISH)
        else:
            mode, direction = ("continuation", Direction.BEARISH) if not uptrend else ("reversal", Direction.BULLISH)
        dir_label = "buy" if direction == Direction.BULLISH else "sell"

        if mode == "continuation":
            names = ["Asian high/low swept", "1H trend agrees (continuation)", "5M BOS in trend direction",
                     "Unmitigated 5M FVG/OB found", "Zone tapped", "Reaction candle closed"]
            r = [swept, swept]
            if not swept:
                r += [False, False, False, False]
                results.append(ConfluenceCheck("Asians (continuation)", symbol, dir_label, names, r))
                continue

            bos_idx = None
            for i in range(sweep_idx + 1, len(post_asian)):
                swings = find_swings(post_asian[: i + 1], width=1)
                ref_swing = last_swing_before(swings, i, is_high=(direction == Direction.BEARISH))
                if ref_swing is None:
                    continue
                c = post_asian[i]
                if direction == Direction.BULLISH and c.close > ref_swing.price:
                    bos_idx = i
                    break
                if direction == Direction.BEARISH and c.close < ref_swing.price:
                    bos_idx = i
                    break
            r.append(bos_idx is not None)

            gap = None
            if bos_idx is not None:
                fvgs = find_fvgs(post_asian, start_index=max(bos_idx - 2, 0))
                mark_mitigated(fvgs, post_asian)
                gap = unmitigated_fvg_in_range(fvgs, post_asian, bos_idx, len(post_asian) - 1, direction)
            r.append(gap is not None)

            tap_idx = _zone_tap(post_asian, gap.top, gap.bottom, gap.formed_at_index + 1) if gap else None
            r.append(tap_idx is not None)

            reaction_idx = _reaction_candle(post_asian, tap_idx, direction) if tap_idx is not None else None
            r.append(reaction_idx is not None)

            results.append(ConfluenceCheck("Asians (continuation)", symbol, dir_label, names, r))

        else:  # reversal
            names = ["Asian high/low swept", "1H trend disagrees (reversal)", "15M FVG/OB zone found",
                     "5M CHoCH (close beyond swing)", "Zone tapped", "Reaction candle closed"]
            r = [swept, swept]
            if not swept:
                r += [False, False, False, False]
                results.append(ConfluenceCheck("Asians (reversal)", symbol, dir_label, names, r))
                continue

            zone_direction = Direction.BEARISH if swept_is_high else Direction.BULLISH
            fvgs_15 = find_fvgs(c15)
            mark_mitigated(fvgs_15, c15)
            candidate_zones = [g for g in fvgs_15 if g.direction == zone_direction and not g.mitigated]
            zone = candidate_zones[-1] if candidate_zones else None
            r.append(zone is not None)

            choch_idx = None
            if zone is not None:
                swings = find_swings(post_asian[: sweep_idx + 1], width=1)
                ref_swing = last_swing_before(swings, sweep_idx, is_high=not swept_is_high)
                if ref_swing is not None:
                    choch_idx = _close_beyond(post_asian, ref_swing.price, sweep_idx + 1, direction)
            r.append(choch_idx is not None)

            tap_idx = _zone_tap(post_asian, zone.top, zone.bottom, choch_idx) if choch_idx is not None else None
            r.append(tap_idx is not None)

            reaction_idx = _reaction_candle(post_asian, tap_idx, direction) if tap_idx is not None else None
            r.append(reaction_idx is not None)

            results.append(ConfluenceCheck("Asians (reversal)", symbol, dir_label, names, r))

    return results


# ---------------------------------------------------------------------------
# 2. "London tt"
# ---------------------------------------------------------------------------

def check_london_tt_progress(data: dict) -> list[ConfluenceCheck]:
    results = []
    for symbol in ("NAS100", "XAUUSD"):
        c15 = data.get((symbol, "15M"), [])
        c30 = data.get((symbol, "30M"), [])
        c5 = data.get((symbol, "5M"), [])
        if not c15 or not c30 or not c5:
            continue

        names = ["Asian 15M trend clear", "Pullback >=25% of Asian range",
                 "15M candle closed since London start", "30M candle agrees with Asian trend"]
        r = [False, False, False, False]

        now = utc_now()
        asian_start, asian_end = ASIAN.current_or_most_recent_window(now)
        asian_15 = [c for c in c15 if asian_start <= c.time < asian_end]
        if not asian_15:
            continue
        asian_trend = structure_trend(asian_15, width=1)
        r[0] = asian_trend is not None
        if asian_trend is None:
            results.append(ConfluenceCheck("London tt", symbol, "unknown", names, r))
            continue

        direction = Direction.BULLISH if asian_trend == Direction.BULLISH else Direction.BEARISH
        dir_label = "buy" if direction == Direction.BULLISH else "sell"

        asian_5 = [c for c in c5 if asian_start <= c.time < asian_end]
        if not asian_5:
            results.append(ConfluenceCheck("London tt", symbol, dir_label, names, r))
            continue
        asian_high = max(c.high for c in asian_5)
        asian_low = min(c.low for c in asian_5)
        asian_range = asian_high - asian_low
        if asian_range <= 0:
            results.append(ConfluenceCheck("London tt", symbol, dir_label, names, r))
            continue

        _, pre_london_end = pre_london_window(now)
        pre_london_candles = [c for c in c5 if asian_end <= c.time < pre_london_end]
        if not pre_london_candles:
            results.append(ConfluenceCheck("London tt", symbol, dir_label, names, r))
            continue

        if asian_trend == Direction.BULLISH:
            pullback_extreme = min(c.low for c in pre_london_candles)
            pullback_size = asian_high - pullback_extreme
        else:
            pullback_extreme = max(c.high for c in pre_london_candles)
            pullback_size = pullback_extreme - asian_low
        r[1] = pullback_size >= config.LONDON_TT_MIN_PULLBACK_PCT * asian_range

        london_15 = [c for c in c15 if c.time >= pre_london_end]
        london_30 = [c for c in c30 if c.time >= pre_london_end]
        if r[1] and london_15:
            r[2] = True
        if r[1] and london_30:
            trend_is_bull = asian_trend == Direction.BULLISH
            confirm_30 = london_30[0]
            r[3] = confirm_30.is_bullish if trend_is_bull else confirm_30.is_bearish

        results.append(ConfluenceCheck("London tt", symbol, dir_label, names, r))
    return results


# ---------------------------------------------------------------------------
# 3. "Asian tt"
# ---------------------------------------------------------------------------

def check_asian_tt_progress(data: dict) -> list[ConfluenceCheck]:
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

    names = ["Asian high/low swept", "Causal swing identified", "5M CHoCH (close beyond swing)",
             "Unmitigated FVG found in displacement leg"]
    results = []
    for swept_is_high, level in ((True, asian_high), (False, asian_low)):
        r = [False, False, False, False]
        sweep_idx = next(
            (i for i, c in enumerate(post_asian) if (c.high > level if swept_is_high else c.low < level)),
            None,
        )
        r[0] = sweep_idx is not None
        if sweep_idx is None:
            continue

        direction = Direction.BEARISH if swept_is_high else Direction.BULLISH
        dir_label = "buy" if direction == Direction.BULLISH else "sell"

        swings = find_swings(post_asian[: sweep_idx + 1], width=1)
        ref_swing = last_swing_before(swings, sweep_idx, is_high=not swept_is_high)
        r[1] = ref_swing is not None
        if ref_swing is None:
            results.append(ConfluenceCheck("Asian tt", symbol, dir_label, names, r))
            continue

        choch_idx = _close_beyond(post_asian, ref_swing.price, sweep_idx + 1, direction)
        r[2] = choch_idx is not None
        if choch_idx is None:
            results.append(ConfluenceCheck("Asian tt", symbol, dir_label, names, r))
            continue

        fvgs = find_fvgs(post_asian, start_index=max(sweep_idx - 2, 0))
        mark_mitigated(fvgs, post_asian)
        gap = unmitigated_fvg_in_range(fvgs, post_asian, sweep_idx, choch_idx, direction)
        r[3] = gap is not None

        results.append(ConfluenceCheck("Asian tt", symbol, dir_label, names, r))
    return results


# ---------------------------------------------------------------------------
# 4 & 5. "newyork" / "newyork tt"
# ---------------------------------------------------------------------------

def _newyork_progress(symbol: str, data: dict) -> list[ConfluenceCheck]:
    c1h = data.get((symbol, "1H"), [])
    c1m = data.get((symbol, "1M"), [])
    strategy_name = "newyork" if symbol == "NAS100" else "newyork tt"
    names = ["8AM NY candle range marked", "Next hour closed beyond range", "IFVG formed",
             "Order block found", "Order block confirmed"]
    if not c1h or not c1m:
        return []

    now = utc_now()
    ny_start, ny_end = NEW_YORK_AM.current_or_most_recent_window(now)
    if now >= ny_end:
        return []  # killzone over - stale, not worth reporting

    r = [False, False, False, False, False]
    eight_am_candle = next((c for c in c1h if c.time.astimezone(NY_TZ).hour == 8), None)
    r[0] = eight_am_candle is not None
    if eight_am_candle is None:
        return [ConfluenceCheck(strategy_name, symbol, "unknown", names, r)]

    range_high, range_low = eight_am_candle.high, eight_am_candle.low
    later_1h = [c for c in c1h if c.time > eight_am_candle.time]
    if not later_1h:
        return [ConfluenceCheck(strategy_name, symbol, "unknown", names, r)]
    next_hour = later_1h[0]

    direction = None
    if next_hour.close > range_high:
        direction = Direction.BEARISH
    elif next_hour.close < range_low:
        direction = Direction.BULLISH
    r[1] = direction is not None
    if direction is None:
        return [ConfluenceCheck(strategy_name, symbol, "unknown", names, r)]
    dir_label = "buy" if direction == Direction.BULLISH else "sell"

    working = [c for c in c1m if c.time >= next_hour.time]
    if len(working) < 3:
        return [ConfluenceCheck(strategy_name, symbol, dir_label, names, r)]

    fvgs = find_fvgs(working)
    mark_mitigated(fvgs, working)
    ifvg = detect_ifvg(fvgs, working, from_index=2)
    r[2] = ifvg is not None
    if ifvg is None:
        return [ConfluenceCheck(strategy_name, symbol, dir_label, names, r)]

    ob = find_order_block(working, ifvg.formed_at_index, direction)
    r[3] = ob is not None
    r[4] = ob is not None and ob.confirmed

    return [ConfluenceCheck(strategy_name, symbol, dir_label, names, r)]


def check_newyork_progress(data: dict) -> list[ConfluenceCheck]:
    return _newyork_progress("NAS100", data)


def check_newyork_tt_progress(data: dict) -> list[ConfluenceCheck]:
    results = []
    results += _newyork_progress("XAUUSD", data)
    results += _newyork_progress("EURUSD", data)
    return results


ALL_PROGRESS_CHECKERS = [
    check_asians_progress,
    check_london_tt_progress,
    check_asian_tt_progress,
    check_newyork_progress,
    check_newyork_tt_progress,
]
