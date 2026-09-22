"""Holiday / special-day info for the planned Saturday (filled by localinfo.load_local).

Google's currentOpeningHours already reflects special hours for the next 7 days; this adds what it can't:
dry days (no alcohol), festival crowds, and closures for places whose hours come from regular schedules.
"""
from __future__ import annotations

from pydantic import BaseModel, Field



class HolidayInfo(BaseModel):
    is_holiday: bool = Field(description="true if the date is a public holiday or major festival in this city/state")
    name: str = Field("", description="holiday/festival name, empty if none")
    dry_day: bool = Field(False, description="true if alcohol sales are banned that day in this state")
    likely_closed: list[str] = Field(default_factory=list, description="category keys that commonly close or cut hours on this day")
    busier: bool = Field(False, description="true if malls/markets/landmarks are typically much more crowded")
    note: str = Field("", description="≤20 words for the traveller, empty if nothing special")


