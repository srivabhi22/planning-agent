"""M0 — shared data contracts."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

Energy = Literal["low", "medium", "high"]
ENERGY_NUM = {"low": 0, "medium": 1, "high": 2}


class UserProfile(BaseModel):
    city: str
    area: Optional[str] = None
    budget: Optional[float] = None  # INR total for the group; None = user gave no budget
    group_size: int = 1
    start_time: Optional[str] = None  # "HH:MM" if the user fixed it
    end_time: Optional[str] = None
    duration_hours: Optional[float] = None
    mood: str = "relaxed"
    energy_curve: list[Energy] = Field(default_factory=lambda: ["medium", "medium", "medium", "low"])
    interests: list[str] = Field(default_factory=list)
    hard_constraints: list[str] = Field(default_factory=list)  # e.g. vegetarian, avoid crowds, wheelchair
    soft_preferences: list[str] = Field(default_factory=list)  # e.g. quiet, scenic, live music
    transport: Literal["cab", "auto", "own_vehicle", "walk_metro"] = "cab"
    assumptions: list[str] = Field(default_factory=list)
    priority_types: list[str] = Field(default_factory=list)  # taxonomy keys the user explicitly asked for → included first
    focused: bool = False
    prefer_popular: bool = False  # famous / must-see spots wanted → favour well-known places over hidden gems  # user clearly wants only certain kinds of places → don't diversify

    # derived flags
    @property
    def vegetarian(self) -> bool:
        return any("veg" in c.lower() and "non" not in c.lower() for c in self.hard_constraints)

    @property
    def avoid_crowds(self) -> bool:
        return any("crowd" in c.lower() for c in self.hard_constraints + self.soft_preferences)


class HourWeather(BaseModel):
    temp: float = 27
    rain_prob: float = 0
    precip_mm: float = 0
    uv: float = 0
    aqi: Optional[float] = None
    code: int = 0  # WMO weather code (95-99 = thunderstorm)


class DayContext(BaseModel):
    lat: float
    lon: float
    place_label: str
    date: str  # YYYY-MM-DD (the Saturday)
    is_today: bool
    now: datetime
    start: datetime
    end: datetime
    sunrise: datetime
    sunset: datetime
    weather: dict[int, HourWeather]  # hour -> weather
    weather_summary: str = ""

    def wx(self, hour: int) -> HourWeather:
        return self.weather.get(hour) or HourWeather()


class Place(BaseModel):
    id: str
    name: str
    lat: float
    lon: float
    category: str  # taxonomy key
    source: str = "osm"
    address: Optional[str] = None
    cuisine: Optional[str] = None
    opening_hours_raw: Optional[str] = None
    sat_hours: Optional[list[tuple[int, int]]] = None  # minutes-from-midnight intervals on Saturday; None = unknown
    rating: Optional[float] = None
    reviews: Optional[int] = None
    price_level: Optional[int] = None  # 0..4
    veg_friendly: Optional[bool] = None
    serves_meals: Optional[bool] = None  # Google servesLunch/servesDinner (bars that double as restaurants)
    maps_url: Optional[str] = None
    summary: Optional[str] = None
    distance_km: float = 0
    # enriched
    vibe_tags: list[str] = Field(default_factory=list)
    energy: Energy = "low"
    duration_min: int = 60
    cost_min: float = 0  # per person INR
    cost_max: float = 0
    indoor: bool = True
    crowd_level: float = 0.5  # 0..1 baseline Saturday crowd
    enriched_by: str = "prior"
    hours_source: Optional[str] = None  # "date" (Google special/current hours), "regular", "osm", None = category default
    web_mentions: int = 0  # times recommended in web search results (blogs, lists, reddit)
    web_note: Optional[str] = None

    @property
    def cost_mid(self) -> float:
        return (self.cost_min + self.cost_max) / 2


class Slot(BaseModel):
    id: str
    type: Literal["breakfast", "morning", "lunch", "afternoon", "snack", "sunset", "evening", "dinner", "night"]
    start: datetime  # ideal arrival
    earliest: datetime
    latest: datetime  # latest arrival
    target_energy: Energy
    is_meal: bool
    categories: list[str] = Field(default_factory=list)
    note: str = ""


class Candidate(BaseModel):
    place: Place
    score: float
    breakdown: dict[str, float]


class Leg(BaseModel):
    mode: Literal["walk", "cab", "auto", "drive", "metro", "bus"]
    minutes: int
    km: float
    cost: float
    depart: datetime
    source: str = "speed-profile"
    traffic_note: str = ""


class Stop(BaseModel):
    slot_id: str
    slot_type: str
    place: Place
    arrive: datetime
    start: datetime
    depart: datetime
    leg_in: Optional[Leg] = None
    cost_min: float = 0
    cost_max: float = 0
    score: float = 0
    breakdown: dict[str, float] = Field(default_factory=dict)
    reason: str = ""
    tip: str = ""


class Plan(BaseModel):
    stops: list[Stop]
    score: float
    penalties: list[str] = Field(default_factory=list)
    cost_min: float = 0
    cost_max: float = 0
    travel_cost: float = 0
    total_minutes: int = 0
    travel_minutes: int = 0
    cost_notes: list[str] = Field(default_factory=list)
    title: str = ""
    summary: str = ""
    tradeoffs: list[str] = Field(default_factory=list)
    tips: list[str] = Field(default_factory=list)

    def signature(self) -> tuple[str, ...]:
        return tuple(s.place.id for s in self.stops)


class TraceEvent(BaseModel):
    step: str
    input: str = ""
    output: str = ""
    decisions: list[str] = Field(default_factory=list)
