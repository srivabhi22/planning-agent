"""M2 — Context Builder: geocode, Saturday time window, hourly weather, AQI, sunset."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from .models import DayContext, HourWeather, UserProfile
from .util import HTTP, cache_get, cache_set, log

DAY_START, DAY_END = (7, 0), (22, 30)  # a realistic "full day" ends ~10:30 PM


def _google_find(query: str) -> tuple[float, float, str] | None:
    """Google Places text search — finds schools, offices, landmarks, societies that OpenStreetMap often lacks."""
    key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not key:
        return None
    r = HTTP.post("https://places.googleapis.com/v1/places:searchText",
                  headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": "places.displayName,places.location,places.formattedAddress"},
                  json={"textQuery": query, "maxResultCount": 1, "regionCode": "IN"})
    r.raise_for_status()
    pl = (r.json().get("places") or [None])[0]
    if not pl:
        return None
    name = (pl.get("displayName") or {}).get("text") or query
    if not _matches(query, f"{name} {pl.get('formattedAddress', '')}"):
        return None  # Google always returns *something*; reject results unrelated to what was typed
    return pl["location"]["latitude"], pl["location"]["longitude"], name


def _matches(query: str, found: str) -> bool:
    """At least one meaningful word of the typed place (before the city) appears in the result's name/address."""
    import re
    norm = lambda t: re.sub(r"[^a-z0-9 ]", " ", t.lower())
    words = [w for w in norm(query.split(",")[0]).split() if len(w) > 2 and w not in {"the", "near", "sector", "road", "nagar"}]
    hay = norm(found)
    return not words or any(w in hay or w.rstrip("s") in hay for w in words)


def _osm_find(query: str) -> tuple[float, float, str] | None:
    r = HTTP.get("https://nominatim.openstreetmap.org/search", params={"q": query, "format": "json", "limit": 1})
    r.raise_for_status()
    js = r.json()
    if not js or not _matches(query, js[0].get("display_name", "")):
        return None
    return float(js[0]["lat"]), float(js[0]["lon"]), js[0]["display_name"].split(",")[0]


def _find(query: str):
    for finder in (_google_find, _osm_find):
        try:
            found = finder(query)
        except Exception as e:  # rate limits / outages must not look like "location not found"
            log.warning("Geocoding via %s failed for %r: %s", finder.__name__, query, str(e)[:150])
            continue
        if found:
            return found
    return None


def geocode(city: str, area: str | None, notes: list | None = None) -> tuple[float, float, str]:
    """Exact start point inside the city (Google → OpenStreetMap), else the city centre (with a note).
    Raises only if even the city can't be found — then the user is asked."""
    from .util import haversine_km
    q = f"{area}, {city}" if area else city
    hit = cache_get("geocode", q.lower())
    if hit:
        return hit[0], hit[1], hit[2]
    c = _find(city)
    if not c:
        raise ValueError(f"Could not geocode city '{city}'")
    out = (c[0], c[1], city)
    if area:
        a = _find(q)
        # the area must actually be in (or right next to) that city — rejects same-named places elsewhere
        if a and haversine_km(a[0], a[1], c[0], c[1]) <= 60:
            out = (a[0], a[1], f"{a[2]}, {city}" if city.lower() not in a[2].lower() else a[2])
        elif notes is not None:
            notes.append(f"Couldn't pinpoint “{area}” in {city} — planning from central {city}")
    cache_set("geocode", q.lower(), out)
    return out


def _hm(s: str) -> tuple[int, int]:
    s = s.strip().lower().replace(".", ":")
    pm = "pm" in s
    am = "am" in s
    s = s.replace("am", "").replace("pm", "").strip()
    h, _, m = s.partition(":")
    h, m = int(h), int(m or 0)
    if pm and h < 12:
        h += 12
    if am and h == 12:
        h = 0
    return h, m


def _round_up_half_hour(dt: datetime) -> datetime:
    dt = dt.replace(second=0, microsecond=0)
    add = (30 - dt.minute % 30) % 30
    return dt + timedelta(minutes=add)


class LocationNotFound(Exception):
    pass


def build_context(p: UserProfile, trace) -> DayContext:
    try:
        lat, lon, label = geocode(p.city, p.area, p.assumptions)
    except Exception as e:
        raise LocationNotFound(f"{p.area}, {p.city}") from e
    trace("Context", f"{p.area or ''} {p.city}".strip(), f"Geocoded to {label} ({lat:.4f}, {lon:.4f})")

    # Forecast (tz=auto gives local times + utc offset)
    wx_ok = True
    try:
        wx = HTTP.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 14,
            "hourly": "temperature_2m,precipitation_probability,precipitation,uv_index,weather_code",
            "daily": "sunrise,sunset",
        }).json()
        wx["hourly"]["time"]
    except Exception:
        # weather service down → plan without it (no weather-based filtering), and say so
        wx_ok = False
        wx = {"utc_offset_seconds": 19800, "hourly": {"time": []}, "daily": {"time": []}}
    off = timedelta(seconds=wx.get("utc_offset_seconds", 19800))
    now = (datetime.now(timezone.utc) + off).replace(tzinfo=None)
    days_ahead = (5 - now.weekday()) % 7  # Saturday = 5
    # it's Saturday but too late for a real outing → plan next Saturday instead
    if days_ahead == 0 and now + timedelta(minutes=40) > now.replace(hour=DAY_END[0], minute=DAY_END[1]) - timedelta(hours=2):
        days_ahead = 7
        p.assumptions.append("It's already late on Saturday — planned for next Saturday instead")
    sat = (now + timedelta(days=days_ahead)).date()
    is_today = days_ahead == 0
    ds = sat.isoformat()

    base = datetime.combine(sat, datetime.min.time())
    if ds in wx["daily"]["time"]:
        di = wx["daily"]["time"].index(ds)
        sunrise = datetime.fromisoformat(wx["daily"]["sunrise"][di])
        sunset = datetime.fromisoformat(wx["daily"]["sunset"][di])
    else:
        sunrise, sunset = base.replace(hour=6, minute=15), base.replace(hour=18, minute=30)
    if not wx_ok:
        p.assumptions.append("Weather forecast unavailable right now — plan doesn't account for rain")

    hours: dict[int, HourWeather] = {}
    H = wx["hourly"]
    for i, t in enumerate(H["time"]):
        if t.startswith(ds):
            h = int(t[11:13])
            hours[h] = HourWeather(temp=H["temperature_2m"][i] or 27, rain_prob=H["precipitation_probability"][i] or 0,
                                   precip_mm=H["precipitation"][i] or 0, uv=H["uv_index"][i] or 0,
                                   code=(H.get("weather_code") or [0] * len(H["time"]))[i] or 0)
    start, end, why = _time_window(p, sat, now, is_today, sunset)
    ctx = DayContext(lat=lat, lon=lon, place_label=label, date=ds, is_today=is_today, now=now,
                     start=start, end=end, sunrise=sunrise, sunset=sunset, weather=hours)
    ctx.weather_summary = summarize_weather(ctx)
    trace("Context", f"Saturday {ds}{' (today)' if is_today else ''}",
          f"Window {start:%I:%M %p} – {end:%I:%M %p}; sunset {sunset:%I:%M %p}. {ctx.weather_summary}", why)
    return ctx


def _time_window(p: UserProfile, sat, now, is_today, sunset):
    why = []
    base = datetime.combine(sat, datetime.min.time())
    day_s = base.replace(hour=DAY_START[0], minute=DAY_START[1])
    day_e = base.replace(hour=DAY_END[0], minute=DAY_END[1])
    earliest = day_s
    if is_today:
        earliest = max(day_s, _round_up_half_hour(now + timedelta(minutes=40)))
        why.append(f"It's Saturday now — earliest start is {earliest:%I:%M %p} (now + ~40 min prep/travel, rounded up)")

    if p.start_time:
        h, m = _hm(p.start_time)
        s = max(earliest, base.replace(hour=h, minute=m))
        e = base.replace(hour=_hm(p.end_time)[0], minute=_hm(p.end_time)[1]) if p.end_time else (
            s + timedelta(hours=p.duration_hours) if p.duration_hours else day_e)
        return s, min(e, day_e), why + [f"Starting {s:%I:%M %p}" + (f" for {p.duration_hours:g} h" if p.duration_hours else ", full day")]
    if p.end_time and p.duration_hours:
        h, m = _hm(p.end_time)
        e = base.replace(hour=h, minute=m)
        return max(earliest, e - timedelta(hours=p.duration_hours)), e, why
    if not p.duration_hours:
        return earliest, day_e, why + ["No duration given — planning a full day"]

    dur = timedelta(hours=p.duration_hours)
    ints = {i.lower() for i in p.interests}
    tired = p.energy_curve[0] == "low"
    # pick the best window for the interests
    if ints & {"music", "nightlife", "bars", "comedy"}:
        if ints & {"walks", "nature", "sunset", "photography"}:
            # catch golden hour for the walk, then music in the evening
            s = sunset - timedelta(minutes=75)
            why.append("Walks + music → start before sunset for a golden-hour walk, finish with evening music")
        else:
            s = base.replace(hour=22, minute=30) - dur
            why.append("Music/nightlife interests → evening window")
    elif ints & {"walks", "nature", "sunset", "photography", "parks"}:
        s = sunset - dur * 0.55
        why.append("Walks/nature → window anchored around sunset (avoids midday heat)")
    elif ints & {"food"} and dur <= timedelta(hours=3):
        s = base.replace(hour=12, minute=0)
        why.append("Food-focused short outing → lunch window")
    else:
        s = base.replace(hour=10 if tired else 9, minute=30 if tired else 0)
        why.append("General interests → late-morning start" if tired else "General interests → morning start")
    if tired and s.hour < 10:
        s = base.replace(hour=10)
        why.append("Tired mood → no early start")
    s = _round_up_half_hour(max(earliest, s))
    e = s + dur
    if e > day_e:
        e = day_e
        s = max(earliest, e - dur)
    return s, e, why


def summarize_weather(ctx: DayContext) -> str:
    hrs = [h for h in range(ctx.start.hour, min(ctx.end.hour + 1, 24))]
    if not hrs:
        return ""
    ws = [ctx.wx(h) for h in hrs]
    tmax, tmin = max(w.temp for w in ws), min(w.temp for w in ws)
    rainy = [h for h, w in zip(hrs, ws) if w.rain_prob >= 50]
    parts = [f"{tmin:.0f}–{tmax:.0f}°C"]
    if rainy:
        parts.append(f"rain likely {_fmt_h(rainy[0])}–{_fmt_h(rainy[-1] + 1)}")
    else:
        parts.append("no significant rain expected")
    storms = [h for h, w in zip(hrs, ws) if w.code >= 95]
    if storms:
        parts.append(f"thunderstorms possible around {_fmt_h(storms[0])}")
    return ", ".join(parts)


def _fmt_h(h: int) -> str:
    return datetime(2000, 1, 1, h % 24).strftime("%I %p").lstrip("0")


def outdoor_ok(ctx: DayContext, hour: int) -> tuple[bool, str]:
    """Only rain or storms rule out outdoor stops (heat/UV/AQI don't eliminate places)."""
    w = ctx.wx(hour)
    if w.code >= 95:
        return False, f"thunderstorm risk at {_fmt_h(hour)}"
    if w.rain_prob >= 55 or w.precip_mm >= 1.5:
        return False, f"rain {w.rain_prob:.0f}% at {_fmt_h(hour)}"
    return True, ""
