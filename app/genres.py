"""M3 — Genre Planner: day skeleton (code) + genre choice per slot (LLM, from a fixed taxonomy)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from pydantic import BaseModel, Field

from .context import outdoor_ok
from .models import DayContext, Slot, UserProfile
from .util import llm_parse

# key: osm filters, google types, indoor, energy, duration(min), cost per person INR (min,max), meal?, interests, good hours
TAXONOMY: dict[str, dict] = {
    "cafe":         dict(osm=['["amenity"="cafe"]'], google=["cafe", "coffee_shop"], indoor=True, energy="low", dur=60, cost=(200, 500), meal="light", interests=["food", "books", "coffee"], hours=(8, 22)),
    "breakfast":    dict(osm=['["amenity"="restaurant"]["cuisine"~"indian|south_indian|breakfast|regional",i]'], google=["breakfast_restaurant", "brunch_restaurant"], indoor=True, energy="low", dur=50, cost=(120, 350), meal="meal", interests=["food"], hours=(7, 11)),
    "restaurant":   dict(osm=['["amenity"="restaurant"]'], google=["restaurant", "vegetarian_restaurant", "indian_restaurant"], indoor=True, energy="low", dur=70, cost=(350, 900), meal="meal", interests=["food"], hours=(12, 23)),
    "street_food":  dict(osm=['["amenity"="fast_food"]'], google=["fast_food_restaurant", "food_court"], indoor=False, energy="medium", dur=40, cost=(100, 300), meal="light", interests=["food"], hours=(11, 23)),
    "bakery_dessert": dict(osm=['["shop"="bakery"]', '["amenity"="ice_cream"]', '["shop"="confectionery"]'], google=["bakery", "ice_cream_shop", "dessert_shop"], indoor=True, energy="low", dur=30, cost=(100, 300), meal="snack", interests=["food"], hours=(10, 23)),
    "park":         dict(osm=['["leisure"="park"]["name"]'], google=["park"], indoor=False, energy="medium", dur=60, cost=(0, 30), meal=None, interests=["walks", "nature", "parks"], hours=(6, 19)),
    "garden_lake":  dict(osm=['["leisure"="garden"]["name"]', '["natural"="water"]["water"="lake"]["name"]', '["leisure"="nature_reserve"]["name"]'], google=["botanical_garden", "garden", "nature_preserve"], indoor=False, energy="medium", dur=60, cost=(0, 50), meal=None, interests=["walks", "nature", "photography", "sunset"], hours=(6, 19)),
    "viewpoint":    dict(osm=['["tourism"="viewpoint"]'], google=["observation_deck"], indoor=False, energy="low", dur=40, cost=(0, 50), meal=None, interests=["walks", "sunset", "photography"], hours=(6, 20)),
    "heritage":     dict(osm=['["historic"~"monument|castle|fort|palace|memorial"]["name"]', '["tourism"="attraction"]["historic"]'], google=["historical_landmark", "historical_place", "monument"], indoor=False, energy="medium", dur=60, cost=(0, 300), meal=None, interests=["history", "culture", "walks", "photography"], hours=(9, 18)),
    "museum":       dict(osm=['["tourism"="museum"]'], google=["museum"], indoor=True, energy="medium", dur=75, cost=(50, 300), meal=None, interests=["culture", "history", "art", "science"], hours=(10, 18)),
    "gallery":      dict(osm=['["tourism"="gallery"]'], google=["art_gallery"], indoor=True, energy="low", dur=45, cost=(0, 200), meal=None, interests=["art", "culture"], hours=(11, 19)),
    "arts_centre":  dict(osm=['["amenity"="arts_centre"]', '["amenity"="theatre"]'], google=["performing_arts_theater", "cultural_center"], indoor=True, energy="low", dur=90, cost=(200, 800), meal=None, interests=["culture", "art", "music", "theatre"], hours=(11, 22)),
    "live_music":   dict(osm=['["amenity"="music_venue"]', '["amenity"="pub"]["live_music"="yes"]', '["amenity"="bar"]["live_music"="yes"]'], google=["live_music_venue", "concert_hall"], indoor=True, energy="medium", dur=90, cost=(400, 1200), meal="light", interests=["music", "nightlife"], hours=(18, 24)),
    "bar_pub":      dict(osm=['["amenity"="pub"]', '["amenity"="bar"]'], google=["pub", "bar"], indoor=True, energy="medium", dur=90, cost=(600, 1500), meal="light", interests=["nightlife", "music"], hours=(17, 24)),
    "bookstore":    dict(osm=['["shop"="books"]'], google=["book_store"], indoor=True, energy="low", dur=40, cost=(0, 400), meal=None, interests=["books", "culture"], hours=(10, 21)),
    "market":       dict(osm=['["amenity"="marketplace"]', '["shop"="mall"]'], google=["market", "shopping_mall"], indoor=False, energy="high", dur=60, cost=(0, 500), meal=None, interests=["shopping", "food"], hours=(10, 22)),
    "cinema":       dict(osm=['["amenity"="cinema"]'], google=["movie_theater"], indoor=True, energy="low", dur=150, cost=(250, 500), meal=None, interests=["movies"], hours=(10, 24)),
    "games":        dict(osm=['["leisure"~"bowling_alley|amusement_arcade|escape_game"]'], google=["bowling_alley", "amusement_center"], indoor=True, energy="high", dur=75, cost=(400, 900), meal=None, interests=["games", "fun"], hours=(11, 23)),
}

# broad families for a balanced day (food is covered by meal slots)
FAMILIES = {
    "nature": ["garden_lake", "park", "viewpoint"],
    "landmark": ["heritage", "museum", "gallery", "arts_centre"],
    "leisure": ["market", "bookstore", "live_music", "games", "cinema"],
}
FAMILY_OF = {c: f for f, cs in FAMILIES.items() for c in cs}

MEAL_CATS = {k for k, v in TAXONOMY.items() if v["meal"] in ("meal",)}
FOOD_CATS = {k for k, v in TAXONOMY.items() if v["meal"]}


# ---------------- skeleton (deterministic) ----------------
# meal anchors: (type, ideal time, earliest, latest arrival, minutes)
MEALS = [("breakfast", (8, 45), (7, 45), (10, 0), 50), ("lunch", (13, 0), (12, 15), (14, 30), 70),
         ("snack", (16, 45), (16, 0), (17, 45), 40), ("dinner", (20, 0), (19, 15), (21, 15), 75)]
MEAL_OPTIONS = {"breakfast": ["breakfast", "cafe"], "lunch": ["restaurant", "street_food"],
                "snack": ["cafe", "bakery_dessert", "street_food"], "dinner": ["restaurant"]}


def _activity_type(t: datetime, sunset: datetime) -> str:
    if sunset - timedelta(minutes=80) <= t <= sunset + timedelta(minutes=10):
        return "sunset"
    if t.hour < 12:
        return "morning"
    if t < sunset - timedelta(minutes=80):
        return "afternoon"
    return "evening" if t.hour < 21 else "night"


def build_skeleton(p: UserProfile, ctx: DayContext) -> list[Slot]:
    S, E = ctx.start, ctx.end
    base = S.replace(hour=0, minute=0, second=0, microsecond=0)
    at = lambda hm: base + timedelta(hours=hm[0], minutes=hm[1])
    total_h = (E - S).total_seconds() / 3600

    # 1) meals the window actually covers
    meals = []
    for typ, ideal, lo, hi, dur in MEALS:
        if S <= at(hi) - timedelta(minutes=20) and E >= at(lo) + timedelta(minutes=dur):
            t = min(max(at(ideal), S), at(hi), E - timedelta(minutes=dur))  # must finish before the day ends
            if t < at(lo) - timedelta(minutes=15):
                continue
            meals.append([typ, t, min(at(lo), t), at(hi), dur])
    # snack only on longer days and not crowding another meal
    meals = [m for m in meals if m[0] != "snack" or (total_h >= 6 and all(
        abs((m[1] - o[1]).total_seconds()) >= 2.5 * 3600 for o in meals if o[0] != "snack"))]
    # short outings: at most one proper meal (the one nearest the middle)
    if total_h < 4 and len(meals) > 1:
        mid = S + (E - S) / 2
        meals = [min(meals, key=lambda m: abs((m[1] - mid).total_seconds()))]

    # 2) fill the gaps between meals with activities (~100 min each incl. travel; tired → fewer)
    per = 120 if p.energy_curve.count("low") >= 3 else 100
    slots: list[tuple] = []
    cursor = S
    for m in meals + [None]:
        gap_end = m[1] if m else E
        gap = (gap_end - cursor).total_seconds() / 60 - (20 if m else 0)
        n = int(gap // per) + (1 if gap % per >= 70 else 0)  # a 70+ min gap still fits one short stop
        for i in range(n):
            t = cursor + timedelta(minutes=gap / n * i) if n else cursor
            slots.append(("activity", t, None, None))
        if m:
            slots.append(("meal", m[1], m[2], m[3], m[0]))
            cursor = m[1] + timedelta(minutes=m[4] + 15)

    out = []
    for i, sl in enumerate(slots):
        frac = i / max(1, len(slots) - 1)
        energy = p.energy_curve[min(3, int(frac * 3.999))]
        if sl[0] == "meal":
            out.append(Slot(id=f"s{i}", type=sl[4], start=sl[1], earliest=sl[2], latest=sl[3], target_energy=energy, is_meal=True))
        else:
            t = sl[1]
            out.append(Slot(id=f"s{i}", type=_activity_type(t, ctx.sunset), start=t, earliest=t - timedelta(minutes=30),
                            latest=t + timedelta(minutes=60), target_energy=energy, is_meal=False))
    return out


# ---------------- genre choice (LLM) ----------------
class _SlotGenres(BaseModel):
    slot_id: str
    categories: list[str] = Field(description="1-3 taxonomy keys, most suitable first")
    note: str = Field(description="one short reason")


class WebPick(BaseModel):
    name: str = Field(description="exact place name as written in the web results")
    category: str = Field(description="taxonomy key")
    why: str = Field(description="≤12 words: what the sources say is good about it")


class _GenrePlan(BaseModel):
    slots: list[_SlotGenres]
    picks: list[WebPick] = Field(default_factory=list, description="specific real places recommended in the web results that fit the chosen genres; max 5 per genre; skip closed places, generic chain mentions and other cities")
    drop_slot_ids: list[str] = Field(default_factory=list, description="slots to drop if the day is over-packed for this mood")


WEB_RULE = """
Also, from the web results, list specific named places that are recommended and fit the genres you chose (picks)."""

SYSTEM = """You are planning what KINDS of places fit each time slot of someone's Saturday. Choose only from the taxonomy keys given.
Think like a local friend: match the mood/energy target, the person's interests, the weather at that hour, and the time of day.
- Meal slots (is_meal=true): breakfast → breakfast/cafe; lunch → restaurant/street_food; snack → cafe/bakery_dessert/street_food; dinner → restaurant (live_music or bar_pub allowed for dinner if the user wants music/nightlife).
- Activity slots (is_meal=false) must NOT use food categories (cafe, restaurant, street_food, bakery_dessert, breakfast) — food belongs only in meal slots.
- Tired/low energy → low-effort genres (cafe, gallery, garden_lake, viewpoint, bookstore, arts_centre), avoid 'games' and 'market'.
- Rain or heat at that hour → indoor genres. A 'sunset' slot should get scenic outdoor genres (viewpoint, garden_lake, park) unless it is raining.
- 'avoid crowds' → avoid market, and prefer smaller venues.
- EVERY stated interest must be the FIRST choice of at least one slot at a time it works (live music → evening/dinner; lakes/parks → daylight, not rain; cafes → anytime). Don't repeat the same genre in consecutive slots.
- If profile.focused is false, aim for a balanced day across activity slots: something outdoors/nature, a landmark or culture spot, and a leisure spot — alongside the user's stated interests, where timing and weather allow. If focused is true, stick to what they asked for.
- For dinner, live_music / bar_pub are fine as first choice if the user wants music (these venues serve food).
- Evening/night slots suit live_music, arts_centre, bar_pub (if no 'no alcohol'), cafe, bakery_dessert."""


def plan_genres(p: UserProfile, ctx: DayContext, slots: list[Slot], trace, snippets: list[dict] | None = None) -> tuple[list[Slot], list[WebPick]]:
    tax_desc = {k: {"indoor": v["indoor"], "energy": v["energy"], "food": v["meal"], "interests": v["interests"]} for k, v in TAXONOMY.items()}
    slot_desc = []
    for s in slots:
        ok, why = outdoor_ok(ctx, s.start.hour)
        slot_desc.append({"slot_id": s.id, "type": s.type, "time": s.start.strftime("%H:%M"), "is_meal": s.is_meal,
                          "target_energy": s.target_energy, "outdoor_ok": ok, "weather_issue": why})
    user = (f"Profile: {p.model_dump_json(include={'mood', 'interests', 'hard_constraints', 'soft_preferences', 'budget', 'group_size', 'focused'})}\n"
            f"Weather: {ctx.weather_summary}. Sunset {ctx.sunset:%H:%M}.\nSlots: {slot_desc}\nTaxonomy: {tax_desc}\n"
            f"Web results (for picks; the user starts from {p.area}, {p.city}): {snippets or '(none)'}")
    picks: list[WebPick] = []
    try:
        gp = llm_parse(SYSTEM + WEB_RULE, user, _GenrePlan)
        picks = gp.picks
        by = {g.slot_id: g for g in gp.slots}
        drop = set(gp.drop_slot_ids) if len(slots) - len(gp.drop_slot_ids) >= 2 else set()
        # never drop a slot if it leaves > ~2h per remaining stop (that's a dead gap, not slack)
        window_min = (ctx.end - ctx.start).total_seconds() / 60
        if drop and window_min / (len(slots) - len(drop)) > 130:
            drop = set()
        out = []
        for s in slots:
            if s.id in drop:
                continue
            g = by.get(s.id)
            cats = [c for c in (g.categories if g else []) if c in TAXONOMY]
            s.categories = cats or _fallback(p, ctx, s)
            s.note = g.note if g else "fallback genres"
            out.append(s)
        if drop:
            trace("Genre Planner", "", f"Dropped {len(drop)} slot(s) to keep the day unhurried", [])
        slots = out
    except Exception as e:
        for s in slots:
            s.categories = _fallback(p, ctx, s)
            s.note = "rule-based genres"
        trace("Genre Planner", "", f"LLM genre planning failed ({type(e).__name__}: {str(e)[:120]}); used rule-based genres")
    # guarantee every stated interest is reachable from at least one slot, at an hour its places are open
    from .sequencer import _interest_cats
    for it in p.interests:
        ic = _interest_cats(it)
        if not ic or any(set(s.categories) & ic for s in slots):
            continue
        is_food_interest = any(TAXONOMY[c]["meal"] for c in ic) and not ic & {"live_music", "bar_pub"}
        for s in sorted(slots, key=lambda s: s.is_meal != is_food_interest):  # food interests → meal slots
            ok = [c for c in ic if TAXONOMY[c]["hours"][0] <= s.start.hour < TAXONOMY[c]["hours"][1]
                  and (TAXONOMY[c]["indoor"] or outdoor_ok(ctx, s.start.hour)[0])]
            if ok:
                s.categories.insert(0, sorted(ok, key=lambda c: TAXONOMY[c]["meal"] is None, reverse=True)[0])
                s.note += f" (+{it})"
                break
    # outdoor-only slot in rain/heat → add indoor backups so the slot isn't just skipped (dead gap)
    for s in slots:
        if s.is_meal or outdoor_ok(ctx, s.start.hour)[0] or any(TAXONOMY[c]["indoor"] for c in s.categories):
            continue
        used = {c for x in slots for c in x.categories}
        backups = [c for c in ("gallery", "bookstore", "museum", "cafe", "bakery_dessert") if c not in s.categories
                   and TAXONOMY[c]["hours"][0] <= s.start.hour < TAXONOMY[c]["hours"][1]]
        backups.sort(key=lambda c: c in used)  # prefer genres not already elsewhere in the day
        s.categories += backups[:2]
        s.note += " (+indoor backup for weather)"
    # balanced day: unless the user is focused, make sure each family is at least an OPTION somewhere it fits
    # (added as an extra choice, never replacing what the user asked for — the sequencer decides if it fits naturally)
    if not p.focused:
        acts = [s for s in slots if not s.is_meal]
        for fam, fcats in FAMILIES.items():
            if any(FAMILY_OF.get(c) == fam for s in slots for c in s.categories):
                continue
            for s in sorted(acts, key=lambda s: len(s.categories)):
                fit = [c for c in fcats if TAXONOMY[c]["hours"][0] <= s.start.hour < TAXONOMY[c]["hours"][1] - 1
                       and (TAXONOMY[c]["indoor"] or outdoor_ok(ctx, s.start.hour)[0])]
                if fit:
                    s.categories.append(fit[0])
                    s.note += f" (+{fam} option for a balanced day)"
                    break
    # food only in meal slots; meal slots always offer their proper options
    for s in slots:
        if s.is_meal:
            opts = MEAL_OPTIONS[s.type]
            extra = [c for c in s.categories if c in ("live_music", "bar_pub")] if s.type == "dinner" else []
            keep = [c for c in s.categories if TAXONOMY[c]["meal"]]  # incl. interest picks like 'cafe' in a lunch slot
            s.categories = list(dict.fromkeys(keep + opts + extra))
        else:
            s.categories = [c for c in s.categories if not TAXONOMY[c]["meal"] or c in ("live_music", "bar_pub")] or _fallback(p, ctx, s)
    trace("Genre Planner", f"{len(slots)} slots",
          " | ".join(f"{s.start:%I:%M %p} {s.type} → {', '.join(s.categories)}" for s in slots),
          [f"{s.type}: {s.note}" for s in slots])
    cats = {c for sl in slots for c in sl.categories}
    picks = [x for x in picks if x.category in cats]
    if picks:
        trace("Web Picks", f"{len(snippets or [])} web results", f"{len(picks)} recommended places to look up",
              [f"{x.name} ({x.category}): {x.why}" for x in picks[:12]])
    return slots, picks


def _fallback(p: UserProfile, ctx: DayContext, s: Slot) -> list[str]:
    ints = set(i.lower() for i in p.interests)
    ok, _ = outdoor_ok(ctx, s.start.hour)
    if s.is_meal:
        return list(MEAL_OPTIONS[s.type])
    if s.type == "sunset" and ok:
        return ["viewpoint", "garden_lake", "park"]
    picks = [k for k, v in TAXONOMY.items() if (set(v["interests"]) & ints or not ints) and not v["meal"]
             and (v["indoor"] or ok) and v["hours"][0] <= s.start.hour < v["hours"][1]]
    if s.target_energy == "low":
        picks = [k for k in picks if TAXONOMY[k]["energy"] != "high"]
    return picks[:3] or (["gallery", "museum"] if not ok else ["park", "gallery"])
