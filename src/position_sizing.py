"""
Best-effort position sizing at a fixed risk percentage of account
balance (config.RISK_PERCENT, currently 5%).

IMPORTANT ASSUMPTION - verify once live: this assumes your account
currency matches each instrument's quote currency (true for a standard
USD account trading USD-quoted EURUSD/XAUUSD/NAS100, which is the normal
Pepperstone setup). If that's ever not the case, these numbers will be
wrong and should not be trusted without a manual check.

Also flagging: cTrader's ProtoOATrader.balance and ProtoOALightSymbol/
ProtoOASymbol numeric fields are typically scaled by 100 (i.e. divide by
100 to get the real value) - this code assumes that scaling. The very
first time this runs with real credentials, sanity-check the suggested
lot size against what you'd expect by hand before trusting it.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class SymbolSpec:
    lot_size: float       # units per 1.0 lot (already unscaled)
    min_volume: int        # in centilots (1/100 lot), as cTrader reports it
    max_volume: int
    step_volume: int


def suggested_lot_size(balance: float, risk_pct: float, entry: float, sl: float,
                        spec: SymbolSpec) -> Optional[float]:
    """Returns a suggested lot size, or None if it can't be computed
    (e.g. missing data or the risk amount rounds down to less than the
    broker's minimum tradeable size)."""
    if not balance or not spec or spec.lot_size <= 0:
        return None
    distance = abs(entry - sl)
    if distance <= 0:
        return None

    risk_amount = balance * risk_pct
    # Dollar value of a 1.0-price-unit move for one full lot, assuming
    # account currency == quote currency (see module docstring).
    value_per_lot_per_unit = spec.lot_size
    raw_lots = risk_amount / (distance * value_per_lot_per_unit)

    raw_centilots = int(raw_lots * 100)
    step = max(spec.step_volume, 1)
    stepped = (raw_centilots // step) * step
    stepped = max(spec.min_volume, min(spec.max_volume, stepped))
    if stepped <= 0:
        return None
    return stepped / 100.0


def format_size_note(balance: Optional[float], lots: Optional[float], risk_pct: float) -> str:
    if balance is None:
        return "Position size: unavailable (couldn't fetch account balance)."
    if lots is None:
        return f"Position size: unavailable (check symbol volume limits - risking {risk_pct:.0%} of ${balance:,.2f})."
    return f"Suggested size: ~{lots:.2f} lots (risking {risk_pct:.0%} of ${balance:,.2f}) - verify before use."
