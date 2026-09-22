"""Local context for the city + date, in ONE LLM call: how people get around (transport profile) and
whether the Saturday is a holiday / festival / dry day. Both parts are cached (transport per city,
holiday per city+date), so repeat plans usually need zero calls here.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from .genres import TAXONOMY
from .holidays import HolidayInfo
from .models import DayContext, UserProfile
from .travel import DEFAULT, TransportProfile
from .util import cache_get, cache_set, llm_parse, log, run_state


class _Local(BaseModel):
    transport: Optional[TransportProfile] = None
    holiday: Optional[HolidayInfo] = None


SYSTEM = f"""You know Indian cities well. Using the web results where they help (your knowledge otherwise), return what is asked:
- transport: how to get around the city cheaply and quickly. Fares in INR. metro_coverage 'wide' only if the metro is the default for most trips (e.g. Delhi); 'partial' if only some corridors (e.g. Bangalore).
- holiday: whether the given date is a public holiday, major festival or dry day in that city/state. If unsure, not a holiday. likely_closed uses only these keys: {list(TAXONOMY)}.
Leave out a part that isn't asked for."""


def load_local(p: UserProfile, ctx: DayContext, trace) -> None:
    st = run_state()
    city = p.city.strip().lower()
    t_hit, h_hit = cache_get("transport", city), cache_get("holiday", f"{city}|{ctx.date}")
    st.transport = TransportProfile(**t_hit) if t_hit else None
    st.holiday = HolidayInfo(**h_hit) if h_hit else None

    if st.transport is None or st.holiday is None:
        from .places import TAVILY_KEY, tavily
        want, snippets = [], []
        if st.transport is None:
            want.append("transport")
            if TAVILY_KEY:
                snippets += tavily(f"how to get around {p.city} metro auto rickshaw cab fares per km")
        if st.holiday is None:
            want.append("holiday")
            if TAVILY_KEY:
                snippets += tavily(f"is {ctx.date} a public holiday festival or dry day in {p.city} India")
        try:
            out = llm_parse(SYSTEM, f"City: {p.city}\nDate: {ctx.date} (Saturday)\nReturn: {want}\n"
                                    f"Web results: {[{'title': r['title'], 'text': r['content']} for r in snippets] or '(none)'}", _Local)
            if st.transport is None and out.transport:
                st.transport = out.transport
                cache_set("transport", city, out.transport.model_dump())
            if st.holiday is None and out.holiday:
                out.holiday.likely_closed = [c for c in out.holiday.likely_closed if c in TAXONOMY]
                st.holiday = out.holiday
                cache_set("holiday", f"{city}|{ctx.date}", out.holiday.model_dump())
        except Exception as e:
            log.warning("Local context lookup failed, using defaults: %s", str(e)[:200])
    st.transport = st.transport or DEFAULT
    st.holiday = st.holiday or HolidayInfo(is_holiday=False)

    T, H = st.transport, st.holiday
    trace("Local Context", f"{p.city}, {ctx.date}", T.advice,
          [f"Metro: {T.metro_coverage}, autos: {'yes' if T.autos_common else 'no'}, traffic: {T.road_congestion}",
           f"Auto ₹{T.auto_base:.0f} + ₹{T.auto_per_km:.0f}/km · cab ₹{T.cab_base:.0f} + ₹{T.cab_per_km:.0f}/km · metro ₹{T.metro_min_fare:.0f}–{T.metro_max_fare:.0f}",
           (f"{H.name or 'Special day'}: {H.note}" + (" · dry day" if H.dry_day else "")) if (H.is_holiday or H.dry_day) else "Regular Saturday"])
    if H.is_holiday or H.dry_day:
        p.assumptions.append(f"{ctx.date} is {H.name or 'a special day'} — " + (H.note or "some places may keep different hours"))
