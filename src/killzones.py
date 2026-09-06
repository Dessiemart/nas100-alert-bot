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
            end_dt = datetime.combin
