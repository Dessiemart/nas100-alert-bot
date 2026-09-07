"""
ICT killzone session windows.

All four killzones are defined in New York time (the standard, published
hours we agreed on) and converted on the fly to Dessie/Ethiopia time (EAT,
UTC+3, no DST) using Python's zoneinfo - so the auto-adjustment across US
DST changes just falls out of the timezone database, no manual offset
tracking needed.

Standard NY-time hours (confirmed earlier):
    Asian:   20:00 - 00:00
    London:  02:00 - 05:00
    NY AM:   08:30 - 11:00
    NY PM:   13:30 - 16:00
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")
EAT_TZ = ZoneInfo("Africa/Addis_Ababa")  # UTC+3, no DST - same offset as Dessie


@dataclass(frozen=True)
class KillzoneWindow:
    name: str
    start_ny: time
    end_ny: time  # if end < start, the window crosses midnight NY time

    def window_for_date(self, ny_date) -> tuple[datetime, datetime]:
        """Return the (start, end) datetimes in NY time for the killzone
        instance that starts on the given NY calendar date."""
        start_dt = datetime.combine(ny_date, self.start_ny, tzinfo=NY_TZ)
        if self.end_ny <= self.start_ny:
            end_dt = datetime.combine(ny_date + timedelta(days=1), self.end_ny, tzinfo=NY_TZ)
        else:
            end_dt = datetime.combine(ny_date, self.end_ny, tzinfo=NY_TZ)
        return start_dt, end_dt

    def contains(self, moment_utc: datetime) -> bool:
        moment_ny = moment_utc.astimezone(NY_TZ)
        for candidate_date in (moment_ny.date() - timedelta(days=1), moment_ny.date()):
            start_dt, end_dt = self.window_for_date(candidate_date)
            if start_dt <= moment_ny < end_dt:
                return True
        return False

    def current_or_most_recent_window(self, moment_utc: datetime) -> tuple[datetime, datetime]:
        """The killzone window (start, end in UTC) that is either currently
        active, or - if none is active right now - the most recently
        completed one. Used so strategies can still reference "today's
        Asian range" shortly after the session has closed."""
        moment_ny = moment_utc.astimezone(NY_TZ)
        candidates = []
        for candidate_date in (moment_ny.date() - timedelta(days=2), moment_ny.date() - timedelta(days=1), moment_ny.date()):
            start_dt, end_dt = self.window_for_date(candidate_date)
            if start_dt <= moment_ny:
                candidates.append((start_dt, end_dt))
        start_dt, end_dt = max(candidates, key=lambda pair: pair[0])
        return start_dt.astimezone(ZoneInfo("UTC")), end_dt.astimezone(ZoneInfo("UTC"))


ASIAN = KillzoneWindow("Asian", time(20, 0), time(0, 0))
LONDON = KillzoneWindow("London", time(2, 0), time(5, 0))
NEW_YORK_AM = KillzoneWindow("New York AM", time(8, 30), time(11, 0))
NEW_YORK_PM = KillzoneWindow("New York PM", time(13, 30), time(16, 0))

ALL_KILLZONES = [ASIAN, LONDON, NEW_YORK_AM, NEW_YORK_PM]


def active_killzones(moment_utc: datetime) -> list[KillzoneWindow]:
    return [kz for kz in ALL_KILLZONES if kz.contains(moment_utc)]


def to_dessie(moment_utc: datetime) -> datetime:
    return moment_utc.astimezone(EAT_TZ)


def pre_london_window(moment_utc: datetime) -> tuple[datetime, datetime]:
    """From Asian killzone end until London killzone start, used by
    'London tt'."""
    asian_start, asian_end = ASIAN.current_or_most_recent_window(moment_utc)
    # London window that starts after this Asian session ended
    moment_ny = asian_end.astimezone(NY_TZ)
    london_start, london_end = LONDON.window_for_date(moment_ny.date())
    if london_start < asian_end.astimezone(NY_TZ):
        london_start, london_end = LONDON.window_for_date(moment_ny.date() + timedelta(days=1))
    return asian_end, london_start.astimezone(ZoneInfo("UTC"))


def is_weekend_market_closed(moment_utc: datetime) -> bool:
    """Forex/CFD markets are closed from Friday 17:00 NY time until
    Sunday 17:00 NY time. Used to skip runs (and API calls) entirely
    over the weekend rather than fetching empty/stale candle data."""
    moment_ny = moment_utc.astimezone(NY_TZ)
    weekday = moment_ny.weekday()  # Monday=0 ... Sunday=6
    if weekday == 5:  # Saturday
        return True
    if weekday == 4 and moment_ny.hour >= 17:  # Friday after 5pm NY
        return True
    if weekday == 6 and moment_ny.hour < 17:  # Sunday before 5pm NY
        return True
    return False
