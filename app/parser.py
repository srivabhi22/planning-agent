"""M1 — Input Parser: form data + raw text -> UserProfile, or clarifying questions when required info is missing.

Required (never assumed): location (area/starting point + city), number of people, interests / place types.
Defaults (no need to ask): start 10 AM, full day; transport = cheapest sensible paid mode for the city; no food or other constraints; no budget limit.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .models import UserProfile
from .util import llm_parse


class _Parsed(BaseModel):
    city: Optional[str] = Field(None, description="city, only if stated or unambiguous from the area")
    area: Optional[str] = Field(None, description="neighbourhood / starting point, only if stated")
    group_size: Optional[int] = Field(None, description="number of people, only if stated or clearly implied ('me and my wife' = 2, 'solo' = 1)")
    start_time: Optional[str] = Field(None, description="HH:MM 24h if a start time was given")
    end_time: Optional[str] = Field(None, description="HH:MM 24h if an end time was given")
    duration_hours: Optional[float] = Field(None, description="length of the outing if given ('4 hours', 'evening' is NOT a duration)")
    full_day: bool = Field(False, description="true only if the user said full day / whole day")
    interests: list[str] = Field(description="kinds of places/activities the user wants, in their words, short (e.g. 'live music', 'lakes', 'south indian food', 'art galleries')")
    mood: Optional[str] = None
    budget: Optional[float] = Field(None, description="total INR budget, ONLY if the user explicitly gave one")
    hard_constraints: list[str] = Field(description="ONLY constraints the user explicitly stated, normalised: 'vegetarian', 'vegan', 'jain', 'avoid crowds', 'no alcohol', 'wheelchair accessible', 'kid friendly'")
    soft_preferences: list[str] = Field(description="stated likes such as 'quiet', 'scenic', 'hidden gems'")
    energy_curve: list[Literal["low", "medium", "high"]] = Field(description="4 values for start, early-mid, late-mid, end. From mood if given, else medium, medium, medium, low")
    focused: bool = Field(description="true ONLY if the user clearly limits the day to certain kinds of places ('only cafes', 'just museums', 'a food crawl', 'pub hopping'). Listing likes ('we love cafes and lakes') is NOT focused.")
    clarifications: list[str] = Field(description="at most 2 questions, ONLY if something the user said is genuinely ambiguous and would change the plan. Do not ask about budget, food or transport. Empty if clear.")


SYSTEM = """You read a request for planning a Saturday outing (usually an Indian city). Input = form fields (may be empty) + free text (may be empty); use both.
If the form and the text genuinely disagree on something that matters (e.g. form says 2 people, text says 'the four of us'; different areas), add a clarification question naming both values instead of picking one.
Extract times exactly as stated (don't "fix" an end time that is before the start time).
Extract ONLY what the user actually said. Never invent a location, group size, time or interest — leave it null/empty so the app can ask.
Do not add constraints (food, budget, accessibility) the user didn't state. Interests should keep the user's specific wording (e.g. 'rooftop cafes', 'lakes', 'jazz')."""


class ServiceBusy(Exception):
    """The LLM is unreachable/over quota and the form alone isn't enough to plan."""


def parse_input(form: dict, free_text: str | None) -> tuple[UserProfile | None, list[str]]:
    """Returns (profile, []) when ready, or (None, questions) when required info is missing."""
    form = {k: v for k, v in (form or {}).items() if v not in (None, "", [], 0)}
    user = f"Form fields:\n{form or '(none)'}\n\nFree text:\n{free_text or '(none)'}"
    try:
        p = llm_parse(SYSTEM, user, _Parsed)
    except Exception:
        # LLM down / over quota: the structured form still works; free text alone can't be read
        if free_text and not all(form.get(k) for k in ("city", "area", "group_size", "interests")):
            raise ServiceBusy()
        p = _from_form(form)
    return _validate(p)


def _validate(p: _Parsed) -> tuple[UserProfile | None, list[str]]:
    q = []
    if not p.area and not p.city:
        q.append("Where will you be starting from? (area and city, e.g. 'Indiranagar, Bangalore')")
    elif not p.area:
        q.append(f"Which area of {p.city} will you start from?")
    elif not p.city:
        q.append(f"Which city is {p.area} in?")
    if p.group_size is None:
        q.append("How many people are going?")
    if not p.interests:
        q.append("What kind of places would you like to visit? (e.g. cafes, lakes, live music, museums, street food)")
    q += _contradictions(p)
    if not q:  # only ask the model's ambiguity questions once the required basics are known (avoids duplicates)
        q = p.clarifications[:2]
    if q:
        return None, q
    assumptions = ["Getting around by the cheapest sensible option for the city (walk / metro / auto / cab)"]
    if not p.start_time and not (p.end_time and p.duration_hours):
        p.start_time = "10:00"
        assumptions.append("No start time given — starting at 10 AM")
    if not (p.duration_hours or (p.start_time and p.end_time)):
        p.full_day = True
        if "full" not in " ".join(assumptions):
            assumptions.append("No duration given — planning the full day")
    curve = (p.energy_curve + ["medium"] * 4)[:4] if p.energy_curve else ["medium", "medium", "medium", "low"]
    prof = UserProfile(
        city=p.city, area=p.area, budget=p.budget, group_size=p.group_size,
        start_time=p.start_time, end_time=p.end_time, duration_hours=None if p.full_day else p.duration_hours,
        mood=p.mood or "", energy_curve=curve, interests=[i.strip().lower() for i in p.interests],
        hard_constraints=p.hard_constraints, soft_preferences=p.soft_preferences, transport="cab", focused=p.focused,
        assumptions=assumptions,
    )
    return prof, []


def _mins(hhmm: Optional[str]) -> Optional[int]:
    try:
        h, m = str(hhmm).strip().split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return None


def _contradictions(p: _Parsed) -> list[str]:
    """Inputs that can't all be true → ask instead of silently picking one."""
    q = []
    s, e = _mins(p.start_time), _mins(p.end_time)
    fmt = lambda m: f"{m // 60 % 12 or 12}:{m % 60:02d} {'AM' if m < 720 else 'PM'}"
    if s is not None and e is not None:
        if e <= s:
            q.append(f"You said you'd start at {fmt(s)} but finish at {fmt(e)}, which is earlier. What are the right start and end times?")
        elif p.duration_hours and abs((e - s) / 60 - p.duration_hours) > 0.75:
            q.append(f"{fmt(s)} to {fmt(e)} is {(e - s) / 60:g} hours, but you also said {p.duration_hours:g} hours. Which one is right?")
    if p.full_day and p.duration_hours and p.duration_hours < 6:
        q.append(f"You mentioned a full day but also {p.duration_hours:g} hours. How much time do you actually have?")
    if s is not None and s >= 22 * 60:
        q.append(f"Starting at {fmt(s)} leaves almost no time on Saturday. Did you mean an earlier start (e.g. {fmt(s - 720)})?")
    if s is not None and p.duration_hours and s + p.duration_hours * 60 > 24 * 60 + 60:
        q.append(f"Starting at {fmt(s)} for {p.duration_hours:g} hours runs well past midnight. Should I plan a shorter outing or an earlier start?")
    if p.duration_hours is not None and p.duration_hours <= 0:
        q.append("How many hours do you have for the outing?")
    if p.group_size is not None and p.group_size <= 0:
        q.append("How many people are going?")
    if p.budget is not None and p.budget <= 0:
        q.append("What budget should I plan around (or should I ignore budget)?")
    return q


def _from_form(form: dict) -> _Parsed:
    """LLM unavailable → structured form only (still no assumptions)."""
    import re
    dur = None
    s = str(form.get("available_time") or "").lower()
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if m:
        dur = float(m.group(1))
    def lst(v): return [x.strip() for x in str(v or "").split(",") if x.strip()]
    return _Parsed(city=form.get("city"), area=form.get("area"), group_size=int(form["group_size"]) if form.get("group_size") else None,
                   start_time=form.get("start_time"), duration_hours=dur, full_day="full" in s or "whole" in s,
                   interests=lst(form.get("interests")), budget=float(form["budget"]) if form.get("budget") else None,
                   hard_constraints=lst(form.get("constraints")), soft_preferences=[], energy_curve=[], focused=False, clarifications=[])
