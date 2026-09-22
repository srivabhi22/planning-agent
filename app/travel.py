"""M7 — Travel Time Service.

Per city, a transport profile (from web search + LLM) says which modes make sense and what they cost
(e.g. Delhi: metro everywhere; Bangalore: autos/cabs, metro only near its lines). Each leg picks the most
economical mode by a generalised cost = time value + fare. The final plan is re-checked with Google Routes
(traffic-aware DRIVE and live TRANSIT) at the real departure times.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .context import outdoor_ok
from .models import DayContext, Leg, UserProfile
from .util import HTTP, cache_get, cache_set, haversine_km, log, run_state

GOOGLE_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
TOMTOM_KEY = os.getenv("TOMTOM_API_KEY")

ROAD_FACTOR = 1.4  # straight-line → road distance in dense Indian cities
# Saturday average door-to-door road speed (km/h) by hour for a congested metro city
SAT_SPEED = {6: 30, 8: 24, 9: 20, 11: 19, 13: 20, 16: 17, 17: 15, 18: 13, 19: 12, 20: 13, 21: 17, 22: 22, 23: 27}
WALK_KMH = 4.5
TIME_VALUE = 3.0  # ₹ per person-minute — how much a minute of the user's Saturday is "worth" vs money
BUFFER = {"walk": 2, "auto": 6, "cab": 8, "metro": 0, "bus": 0}


class TransportProfile(BaseModel):
    metro_coverage: Literal["wide", "partial", "none"] = Field(description="wide = metro reaches most areas and is the default way locals travel (e.g. Delhi); partial = only some corridors (e.g. Bangalore); none")
    autos_common: bool = Field(description="auto-rickshaws widely available via street or apps")
    auto_base: float = Field(description="INR minimum auto fare")
    auto_per_km: float
    cab_base: float = Field(description="INR typical app-cab minimum fare")
    cab_per_km: float
    metro_min_fare: float
    metro_max_fare: float
    road_congestion: Literal["low", "medium", "high"] = Field(description="Saturday traffic in the city centre")
    walk_max_km: float = Field(description="comfortable walking distance between stops for locals, usually 0.8-1.5")
    advice: str = Field(description="≤20 words: how locals get around cheaply and quickly")


DEFAULT = TransportProfile(metro_coverage="partial", autos_common=True, auto_base=30, auto_per_km=15, cab_base=60,
                           cab_per_km=20, metro_min_fare=10, metro_max_fare=60, road_congestion="high", walk_max_km=1.2,
                           advice="Autos/cabs for short hops, metro where it runs close by.")


def _P() -> TransportProfile:
    return run_state().transport or DEFAULT


def _speed(when: datetime) -> float:
    ks = sorted(SAT_SPEED)
    s = SAT_SPEED[max([k for k in ks if k <= when.hour], default=ks[0])]
    return s * {"high": 1.0, "medium": 1.25, "low": 1.5}[_P().road_congestion]


def _fare(mode: str, km: float, n: int) -> float:
    P = _P()
    if mode == "walk":
        return 0
    if mode == "auto":
        return (P.auto_base + P.auto_per_km * max(0, km - 1.5)) * math.ceil(n / 3)
    if mode == "cab":
        return (P.cab_base + P.cab_per_km * max(0, km - 1)) * math.ceil(n / 4)
    if mode in ("metro", "bus"):
        return min(P.metro_max_fare, max(P.metro_min_fare, P.metro_min_fare + 2.5 * km)) * n
    return 0


def _gen_cost(minutes: float, fare: float, n: int) -> float:
    return minutes * TIME_VALUE * n + fare


def _options(p: UserProfile, ctx: DayContext, km_line: float, depart: datetime) -> list[tuple[str, float, float]]:
    """(mode, minutes, road_km) heuristics for every sensible mode."""
    km = km_line * ROAD_FACTOR
    walk_ok, _ = outdoor_ok(ctx, depart.hour)
    tired = p.energy_curve.count("low") >= 3
    opts = []
    if km_line < 0.3 or (walk_ok and km_line <= _P().walk_max_km * (0.7 if tired else 1.0)):
        opts.append(("walk", km / WALK_KMH * 60 + BUFFER["walk"], km))
    if km_line >= 0.6:
        road = km / _speed(depart) * 60
        if _P().autos_common and km <= 15:
            opts.append(("auto", road + BUFFER["auto"], km))
        opts.append(("cab", road + BUFFER["cab"], km))
        if _P().metro_coverage != "none" and km_line >= 2.5:
            access = 14 if _P().metro_coverage == "wide" else 24  # walk to/from station + wait
            opts.append(("metro", km / 32 * 60 + access, km))
    return opts


def estimate(p: UserProfile, ctx: DayContext, a: tuple[float, float], b: tuple[float, float], depart: datetime) -> Leg:
    km_line = haversine_km(a[0], a[1], b[0], b[1])
    opts = _options(p, ctx, km_line, depart) or [("cab", km_line * ROAD_FACTOR / _speed(depart) * 60 + 8, km_line * ROAD_FACTOR)]
    n = p.group_size
    mode, mins, km = min(opts, key=lambda o: _gen_cost(o[1], _fare(o[0], o[2], n), n))
    return Leg(mode=mode, minutes=max(3, round(mins)), km=round(km, 2), cost=round(_fare(mode, km, n)), depart=depart)


# -------- live refinement for the final plan --------
def refine(p: UserProfile, ctx: DayContext, a, b, leg: Leg) -> Leg:
    """Compare live options (Google drive w/ traffic, Google transit, walking) and keep the most economical."""
    n = p.group_size
    km_line = haversine_km(a[0], a[1], b[0], b[1])
    cands: list[Leg] = [leg]
    if km_line < 0.8:
        walk_ok, _ = outdoor_ok(ctx, leg.depart.hour)
        if walk_ok or km_line < 0.3:
            km = km_line * ROAD_FACTOR
            return Leg(mode="walk", minutes=max(3, round(km / WALK_KMH * 60 + BUFFER["walk"])), km=round(km, 2), cost=0, depart=leg.depart)
    try:
        drive = _google_drive(ctx, a, b, leg.depart) or _tomtom(ctx, a, b, leg.depart)
    except Exception as e:
        log.warning("Drive routing failed: %s", str(e)[:200]); drive = None
    if drive:
        mins, km, src = drive
        free = km / 30 * 60
        note = f"Traffic adds ~{round(mins - free)} min" if mins - free >= 12 else ""
        for mode in (["auto"] if _P().autos_common and km <= 15 else []) + ["cab"]:
            cands.append(Leg(mode=mode, minutes=round(mins + BUFFER[mode]), km=round(km, 2), cost=round(_fare(mode, km, n)),
                             depart=leg.depart, source=src, traffic_note=note))
    if _P().metro_coverage != "none" and km_line >= 2:
        try:
            tr = _google_transit(ctx, a, b, leg.depart)
        except Exception as e:
            log.warning("Transit routing failed: %s", str(e)[:200]); tr = None
        if tr:
            mins, km, fare, vehicle = tr
            mode = "metro" if vehicle == "metro" else "bus"
            cands.append(Leg(mode=mode, minutes=round(mins), km=round(km, 2),
                             cost=round(fare * n if fare else _fare(mode, km, n)), depart=leg.depart, source="Google Routes (live transit)"))
    live = [c for c in cands if c.source != "speed-profile" or c.mode == "walk"] or cands  # walking needs no live data
    return min(live, key=lambda c: _gen_cost(c.minutes, c.cost, n))


def _utc_iso(ctx: DayContext, local: datetime) -> str:
    off = ctx.now - datetime.now(timezone.utc).replace(tzinfo=None)
    off = timedelta(minutes=round(off.total_seconds() / 60 / 15) * 15)
    return (local - off).strftime("%Y-%m-%dT%H:%M:%SZ")


def _routes(ctx, a, b, depart, mode: str, fields: str) -> Optional[dict]:
    if not GOOGLE_KEY or depart <= ctx.now:
        return None
    key = f"{mode}|{a}|{b}|{depart:%Y%m%d%H}{depart.minute // 15}"
    hit = cache_get("routes", key)
    if hit is not None:
        return hit or None
    body = {"origin": {"location": {"latLng": {"latitude": a[0], "longitude": a[1]}}},
            "destination": {"location": {"latLng": {"latitude": b[0], "longitude": b[1]}}},
            "travelMode": mode, "departureTime": _utc_iso(ctx, depart)}
    if mode == "DRIVE":
        body["routingPreference"] = "TRAFFIC_AWARE_OPTIMAL"
    r = HTTP.post("https://routes.googleapis.com/directions/v2:computeRoutes",
                  headers={"X-Goog-Api-Key": GOOGLE_KEY, "X-Goog-FieldMask": fields}, json=body)
    r.raise_for_status()
    routes = r.json().get("routes") or []
    out = routes[0] if routes else {}
    cache_set("routes", key, out)
    return out or None


def _google_drive(ctx, a, b, depart):
    rt = _routes(ctx, a, b, depart, "DRIVE", "routes.duration,routes.distanceMeters")
    if not rt:
        return None
    return int(rt["duration"].rstrip("s")) / 60, rt["distanceMeters"] / 1000, "Google Routes (traffic-aware)"


def _google_transit(ctx, a, b, depart):
    rt = _routes(ctx, a, b, depart, "TRANSIT",
                 "routes.duration,routes.distanceMeters,routes.travelAdvisory.transitFare,routes.legs.steps.transitDetails.transitLine.vehicle.type")
    if not rt:
        return None
    types = {st.get("transitDetails", {}).get("transitLine", {}).get("vehicle", {}).get("type")
             for lg in rt.get("legs", []) for st in lg.get("steps", [])} - {None}
    if not types:
        return None  # "transit" route that is just walking
    fare = (rt.get("travelAdvisory", {}).get("transitFare") or {})
    fare_inr = float(fare.get("units", 0)) + fare.get("nanos", 0) / 1e9 if fare.get("currencyCode") == "INR" else None
    vehicle = "metro" if types & {"SUBWAY", "METRO_RAIL", "HEAVY_RAIL", "COMMUTER_TRAIN", "RAIL", "MONORAIL"} else "bus"
    return int(rt["duration"].rstrip("s")) / 60, rt.get("distanceMeters", 0) / 1000, fare_inr, vehicle


def _tomtom(ctx, a, b, depart):
    if not TOMTOM_KEY:
        return None
    params = {"key": TOMTOM_KEY, "traffic": "true", "travelMode": "car"}
    if depart > ctx.now:
        params["departAt"] = _utc_iso(ctx, depart)
    r = HTTP.get(f"https://api.tomtom.com/routing/1/calculateRoute/{a[0]},{a[1]}:{b[0]},{b[1]}/json", params=params)
    r.raise_for_status()
    s = r.json()["routes"][0]["summary"]
    return s["travelTimeInSeconds"] / 60, s["lengthInMeters"] / 1000, "TomTom (traffic-aware)"
