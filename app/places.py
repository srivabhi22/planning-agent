"""M4 — Place Retriever (Overpass + optional Google Places) and M5 — Place Enricher (priors + LLM, cached)."""
from __future__ import annotations

import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .genres import TAXONOMY
from .models import DayContext, Place, Slot, UserProfile
from .util import HTTP, cache_get, cache_set, haversine_km, llm_parse, log, maps_link, run_state, submit

GOOGLE_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
BIG_CITIES = {"bangalore", "bengaluru", "mumbai", "delhi", "new delhi", "hyderabad", "chennai", "pune", "kolkata", "gurgaon", "gurugram", "noida"}
PER_CATEGORY_KEEP = 6   # enrich only the strongest few per type (+ every web-recommended place)
ENRICH_BATCH = 40


def search_radius_km(p: UserProfile) -> float:
    # wide enough to reach the good spots; the sequencer (not the radius) keeps the route sensible
    big = p.city.lower() in BIG_CITIES
    f = run_state().radius_factor
    if p.avoid_crowds or p.energy_curve.count("low") >= 3:
        return (9.0 if big else 7.0) * f
    return (14.0 if big else 10.0) * f


# ======================= opening hours =======================
_DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]


def _day_in_spec(spec: str, day: str = "Sa") -> bool:
    idx = _DAYS.index(day)
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")[:2]
            if a in _DAYS and b in _DAYS:
                ia, ib = _DAYS.index(a), _DAYS.index(b)
                if (ia <= idx <= ib) if ia <= ib else (idx >= ia or idx <= ib):
                    return True
        elif part == day:
            return True
    return False


def parse_osm_hours(s: Optional[str]) -> Optional[list[tuple[int, int]]]:
    """Saturday intervals (minutes from midnight) from OSM opening_hours. None if unknown/unparseable."""
    if not s:
        return None
    s = s.strip()
    if s == "24/7":
        return [(0, 1440)]
    result = None
    try:
        for rule in s.split(";"):
            rule = rule.strip()
            if not rule or rule.startswith("PH"):
                continue
            m = re.match(r"^((?:(?:Mo|Tu|We|Th|Fr|Sa|Su)(?:-(?:Mo|Tu|We|Th|Fr|Sa|Su))?,?)+)\s*(.*)$", rule)
            if m:
                days, rest = m.group(1), m.group(2).strip()
                if not _day_in_spec(days):
                    continue
            else:
                rest = rule
            if rest.lower() in ("off", "closed"):
                result = []
                continue
            iv = []
            for a, b in re.findall(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2}|24:00)", rest):
                sa = int(a[:-3]) * 60 + int(a[-2:])
                sb = int(b[:-3]) * 60 + int(b[-2:])
                if sb <= sa:
                    sb += 1440
                iv.append((sa, sb))
            if iv:
                result = iv  # later rules override earlier ones for the same day
    except Exception:
        return None
    return result


def parse_google_current(coh: Optional[dict]) -> Optional[list[tuple[int, int]]]:
    """Hours for the exact planned date from currentOpeningHours (covers holidays / special closures, next 7 days only)."""
    TARGET_DATE = run_state().target_date
    if not coh or not TARGET_DATE:
        return None
    y, m, d = (int(x) for x in TARGET_DATE.split("-"))
    tgt = {"year": y, "month": m, "day": d}
    dated = [pr for pr in coh.get("periods", []) if (pr.get("open") or {}).get("date")]
    if not dated:
        return None  # Google didn't return dated periods → fall back to regular hours
    iv = []
    for pr in dated:
        o, c = pr["open"], pr.get("close")
        if o.get("date") != tgt:
            continue
        sa = o.get("hour", 0) * 60 + o.get("minute", 0)
        if not c:
            iv.append((sa, 1440)); continue
        sb = c.get("hour", 0) * 60 + c.get("minute", 0) + (1440 if c.get("date") != tgt else 0)
        iv.append((sa, sb))
    # the date is inside the 7-day window but has no periods → closed that day (special closure)
    covers = any((pr.get("open") or {}).get("date", {}) and
                 (pr["open"]["date"]["year"], pr["open"]["date"]["month"], pr["open"]["date"]["day"]) >= (y, m, d) for pr in dated)
    return iv if iv or covers else None


def parse_google_hours(roh: Optional[dict]) -> Optional[list[tuple[int, int]]]:
    if not roh or "periods" not in roh:
        return None
    iv = []
    for per in roh["periods"]:
        o, c = per.get("open", {}), per.get("close")
        if o.get("day") != 6:
            continue
        sa = o.get("hour", 0) * 60 + o.get("minute", 0)
        if not c:
            iv.append((0, 1440))
            continue
        sb = c.get("hour", 0) * 60 + c.get("minute", 0) + (1440 if c.get("day") != 6 else 0)
        iv.append((sa, sb))
    return iv


# ======================= retrieval =======================
def _classify_osm(tags: dict, wanted: list[str]) -> Optional[str]:
    for cat in wanted:
        for f in TAXONOMY[cat]["osm"]:
            ok = True
            for k, op, v in re.findall(r'\["([^"]+)"(?:(=|~)"([^"]+)")?(?:,i)?\]', f):
                tv = tags.get(k)
                if op == "":
                    ok &= tv is not None
                elif op == "=":
                    ok &= tv == v
                else:
                    ok &= tv is not None and re.search(v, tv, re.I) is not None
            if ok:
                return cat
    return None


def fetch_osm(lat: float, lon: float, radius_km: float, cats: list[str]) -> list[Place]:
    key = f"{lat:.3f},{lon:.3f},{radius_km},{sorted(cats)}"
    hit = cache_get("overpass", key)
    if hit is None:
        r = int(radius_km * 1000)
        body = "".join(f"nwr{f}(around:{r},{lat},{lon});" for c in cats for f in TAXONOMY[c]["osm"])
        # With Google Places available, OSM is only a supplement → don't let a slow Overpass stall the run
        tmo = 25 if GOOGLE_KEY else 60
        q = f"[out:json][timeout:{tmo}];({body});out center tags 800;"
        hit = []
        try:
            resp = HTTP.get("https://overpass-api.de/api/interpreter", params={"data": q}, timeout=tmo + 5)  # POST now returns 406
            resp.raise_for_status()
            hit = resp.json().get("elements", [])
        except Exception as e:
            log.warning("Overpass failed (%s: %s) — continuing without OSM results", type(e).__name__, str(e)[:200])
        if hit:
            cache_set("overpass", key, hit)  # never cache failures
    out = []
    for el in hit:
        tags = el.get("tags", {})
        name = tags.get("name:en") or tags.get("name")
        la = el.get("lat") or el.get("center", {}).get("lat")
        lo = el.get("lon") or el.get("center", {}).get("lon")
        if not name or la is None:
            continue
        cat = _classify_osm(tags, cats)
        if not cat:
            continue
        diet = tags.get("diet:vegetarian")
        veg = True if diet in ("yes", "only") else (False if diet == "no" else None)
        if tags.get("diet:vegan") in ("yes", "only"):
            veg = True
        out.append(Place(
            id=f"osm:{el['type']}/{el['id']}", name=name, lat=la, lon=lo, category=cat, source="osm",
            address=", ".join(x for x in [tags.get("addr:street"), tags.get("addr:suburb")] if x) or None,
            cuisine=tags.get("cuisine"), opening_hours_raw=tags.get("opening_hours"),
            sat_hours=parse_osm_hours(tags.get("opening_hours")), veg_friendly=veg,
            summary=tags.get("description"),
        ))
    return out


def fetch_google(lat: float, lon: float, radius_km: float, cat: str, p: UserProfile) -> list[Place]:
    if not GOOGLE_KEY:
        return []
    types = TAXONOMY[cat]["google"]
    key = f"g:{lat:.3f},{lon:.3f},{radius_km},{cat}|{run_state().target_date}"
    hit = cache_get("gplaces", key)
    if hit is None:
        fields = G_FIELDS
        try:
            r = HTTP.post("https://places.googleapis.com/v1/places:searchNearby",
                          headers={"X-Goog-Api-Key": GOOGLE_KEY, "X-Goog-FieldMask": fields},
                          json={"includedTypes": types, "maxResultCount": 20, "rankPreference": "POPULARITY",
                                "locationRestriction": {"circle": {"center": {"latitude": lat, "longitude": lon},
                                                                   "radius": min(radius_km * 1000, 50000)}}})
            r.raise_for_status()
            hit = r.json().get("places", [])
        except Exception as e:
            log.warning("Google Places %s failed: %s", cat, str(e)[:200])
            hit = None
        if hit is None:
            return []
        cache_set("gplaces", key, hit)
    out = [x for x in (_g_place(g, cat) for g in hit) if x]
    return out


TAVILY_KEY = os.getenv("TAVILY_API_KEY")
LABEL = {
    "cafe": "cafes", "breakfast": "breakfast places", "restaurant": "restaurants", "street_food": "street food",
    "bakery_dessert": "desserts and bakeries", "park": "parks", "garden_lake": "lakes and gardens", "viewpoint": "viewpoints",
    "heritage": "heritage sites", "museum": "museums", "gallery": "art galleries", "arts_centre": "theatres and cultural centres",
    "live_music": "live music venues", "bar_pub": "pubs and bars", "bookstore": "bookstores", "market": "markets",
    "cinema": "cinemas", "games": "gaming and bowling",
}


# Google place types that genuinely belong to each category (substring match on primaryType/types)
TYPE_OK = {
    "cafe": ["cafe", "coffee", "tea_house", "bakery", "dessert"],
    "breakfast": ["restaurant", "cafe", "bakery", "food", "diner"],
    "restaurant": ["restaurant", "food", "diner", "meal_", "steak", "barbecue"],
    "street_food": ["fast_food", "food_court", "meal_takeaway", "snack", "restaurant", "food_stand"],
    "bakery_dessert": ["bakery", "dessert", "ice_cream", "confectioner", "cafe", "chocolate", "cake", "donut", "juice"],
    "park": ["park", "garden", "hiking", "nature"],
    "garden_lake": ["park", "garden", "lake", "natural_feature", "nature", "botanical", "wildlife"],
    "viewpoint": ["observation_deck", "natural_feature", "park", "scenic", "tourist_attraction", "hiking"],
    "heritage": ["historical", "monument", "landmark", "fort", "palace", "temple", "church", "mosque", "place_of_worship", "tourist_attraction", "museum", "archaeological"],
    "museum": ["museum", "planetarium", "science_center"],
    "gallery": ["art_gallery", "museum", "art_studio", "cultural"],
    "arts_centre": ["performing_arts", "theater", "cultural", "auditorium", "concert", "amphitheat", "event_venue", "art_"],
    "live_music": ["live_music", "concert", "night_club", "bar", "pub", "event_venue", "restaurant", "jazz"],
    "bar_pub": ["bar", "pub", "night_club", "brewery", "brewpub", "wine", "lounge"],
    "bookstore": ["book", "library"],
    "market": ["market", "shopping_mall", "shopping_center", "bazaar"],
    "cinema": ["movie_theater", "cinema"],
    "games": ["bowling", "amusement", "arcade", "escape_room", "gaming", "go_kart", "trampoline"],
}
TYPE_NEVER = ["transit_station", "subway_station", "bus_station", "train_station", "bus_stop", "parking", "school",
              "university", "corporate_office", "real_estate", "hospital", "atm", "bank", "gas_station", "car_", "lodging"]


def _type_fits(g: dict, cat: str) -> bool:
    """Reject Google results whose type doesn't match the category (e.g. a carpet store returned for 'museum')."""
    prim = (g.get("primaryType") or "").lower()
    types = [t.lower() for t in g.get("types") or []]
    ok = TYPE_OK.get(cat, [])
    if prim:
        if any(b in prim for b in TYPE_NEVER):
            return False
        return any(k in prim for k in ok) or (prim in ("point_of_interest", "establishment", "tourist_attraction")
                                              and any(k in t for t in types for k in ok))
    return any(k in t for t in types for k in ok)


def _best_hours(g: dict):
    cur = parse_google_current(g.get("currentOpeningHours"))
    if cur is not None:
        return cur, "date"
    reg = parse_google_hours(g.get("regularOpeningHours"))
    return reg, ("regular" if reg is not None else None)


def _g_place(g: dict, cat: str) -> Optional[Place]:
    if not _type_fits(g, cat):
        return None
    PL = {"PRICE_LEVEL_FREE": 0, "PRICE_LEVEL_INEXPENSIVE": 1, "PRICE_LEVEL_MODERATE": 2, "PRICE_LEVEL_EXPENSIVE": 3, "PRICE_LEVEL_VERY_EXPENSIVE": 4}
    if g.get("businessStatus") not in (None, "OPERATIONAL"):
        return None
    loc = g.get("location", {})
    name = (g.get("displayName") or {}).get("text")
    if not name or "latitude" not in loc:
        return None
    veg = g.get("servesVegetarianFood")
    if g.get("primaryType") == "vegetarian_restaurant":
        veg = True
    return Place(id=f"g:{g['id']}", name=name, lat=loc["latitude"], lon=loc["longitude"], category=cat, source="google",
                 address=g.get("formattedAddress"), rating=g.get("rating"), reviews=g.get("userRatingCount"),
                 price_level=PL.get(g.get("priceLevel")), veg_friendly=veg, maps_url=g.get("googleMapsUri"),
                 sat_hours=_best_hours(g)[0], hours_source=_best_hours(g)[1],
                 summary=(g.get("editorialSummary") or {}).get("text"))


G_FIELDS = ("places.id,places.displayName,places.location,places.rating,places.userRatingCount,places.priceLevel,"
            "places.regularOpeningHours,places.primaryType,places.servesVegetarianFood,places.formattedAddress,"
            "places.googleMapsUri,places.editorialSummary,places.businessStatus,places.types,places.currentOpeningHours")


def google_text(query: str, lat: float, lon: float, radius_km: float, cat: str, n: int = 20) -> list[Place]:
    key = f"t:{query.lower()}|{lat:.3f},{lon:.3f}|{n}|{run_state().target_date}"
    hit = cache_get("gtext", key)
    if hit is None:
        try:
            r = HTTP.post("https://places.googleapis.com/v1/places:searchText",
                          headers={"X-Goog-Api-Key": GOOGLE_KEY, "X-Goog-FieldMask": G_FIELDS},
                          json={"textQuery": query, "maxResultCount": n,
                                "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lon}, "radius": radius_km * 1000}}})
            r.raise_for_status()
            hit = r.json().get("places", [])
            cache_set("gtext", key, hit)
        except Exception as e:
            log.warning("Google text search '%s' failed: %s", query, str(e)[:200])
            return []
    return [x for x in (_g_place(g, cat) for g in hit) if x]


def tavily(query: str) -> list[dict]:
    hit = cache_get("tavily", query.lower())
    if hit is not None:
        return hit
    try:
        r = HTTP.post("https://api.tavily.com/search", headers={"Authorization": f"Bearer {TAVILY_KEY}"},
                      json={"query": query, "search_depth": "basic", "max_results": 6, "include_answer": False})
        r.raise_for_status()
        hit = [{"title": x.get("title"), "url": x.get("url"), "content": (x.get("content") or "")[:700]} for x in r.json().get("results", [])]
        cache_set("tavily", query.lower(), hit)
        return hit
    except Exception as e:
        log.warning("Tavily '%s' failed: %s", query, str(e)[:200])
        return []


def web_snippets(p: UserProfile, trace) -> list[dict]:
    """Tavily searches in the user's own words (+ one for food). Read by the genre planner, which picks named places."""
    if not TAVILY_KEY:
        return []
    queries = [f"best {i} in {p.city}" for i in p.interests[:4]] + [f"best local restaurants and street food in {p.city}"]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(5) as ex:
        results = [f.result() for f in [submit(ex, tavily, q) for q in queries]]
    snippets = [{"query": q, "title": r["title"], "text": r["content"]} for q, rs in zip(queries, results) for r in rs]
    trace("Web Search", "; ".join(queries), f"{len(snippets)} results from blogs, lists and forums")
    return snippets


def retrieve(p: UserProfile, ctx: DayContext, slots: list[Slot], trace, picks: list | None = None) -> list[Place]:
    picks = picks or []
    cats = sorted({c for s in slots for c in s.categories})
    R = search_radius_km(p)
    places: list[Place] = []
    def _norm(n): return re.sub(r"[^a-z0-9]", "", n.lower())[:14]
    if GOOGLE_KEY:
        from concurrent.futures import ThreadPoolExecutor
        from .sequencer import _interest_cats
        with ThreadPoolExecutor(8) as ex:
            # one city-wide quality search per place type (biased toward the start point) …
            futs = [submit(ex, google_text, f"best {LABEL[c]} in {p.city}", ctx.lat, ctx.lon, R, c) for c in cats]
            # … plus one per interest in the user's own words, near where they start
            for it in p.interests:
                ic = sorted(_interest_cats(it) & set(cats))
                if ic:
                    futs.append(submit(ex, google_text, f"{it} near {p.area}, {p.city}", ctx.lat, ctx.lon, R, ic[0]))
            for f in futs:
                places += f.result()
            # web-recommended places: mark the ones already found, look up only the rest
            have = {_norm(x.name): x for x in places}
            missing = []
            for pk in picks:
                hit = have.get(_norm(pk.name))
                if hit:
                    hit.web_mentions, hit.web_note = 1, pk.why
                else:
                    missing.append(pk)
            looked = [(pk, submit(ex, google_text, f"{pk.name}, {p.city}", ctx.lat, ctx.lon, R, pk.category, 1)) for pk in missing[:15]]
            for pk, f in looked:
                for pl in f.result():
                    pl.web_mentions, pl.web_note = 1, pk.why
                    places.append(pl)
        src = "web search + Google Maps" if picks else "Google Maps"
    else:
        places = fetch_osm(ctx.lat, ctx.lon, R, cats)
        src = "OpenStreetMap (no Google key)"

    # merge duplicates (same place found by several searches) — keep the richest record, carry web mentions
    by_key: dict = {}
    for pl in places:
        k = pl.id if pl.source == "google" else (_norm(pl.name), round(pl.lat, 3), round(pl.lon, 3))
        if k in by_key:
            old = by_key[k]
            old.web_mentions = max(old.web_mentions, pl.web_mentions)
            old.web_note = old.web_note or pl.web_note
        else:
            by_key[k] = pl
    uniq, junk = [], 0
    for pl in by_key.values():
        # without reviews or a web mention it's usually not a real outing spot
        if pl.source == "google" and not pl.web_mentions and (pl.reviews or 0) < 25:
            junk += 1
            continue
        pl.distance_km = haversine_km(ctx.lat, ctx.lon, pl.lat, pl.lon)
        if pl.distance_km <= R:
            pl.maps_url = pl.maps_url or maps_link(pl.name, pl.lat, pl.lon)
            uniq.append(pl)
    counts = {c: sum(1 for x in uniq if x.category == c) for c in cats}
    trace("Place Retriever", f"{len(cats)} place types within {R:.0f} km of {ctx.place_label}",
          f"{len(uniq)} places via {src}: " + ", ".join(f"{v} {LABEL[k]}" for k, v in counts.items()),
          [f"{sum(1 for x in uniq if x.web_mentions)} of them are recommended on the web", f"Dropped {junk} low-signal results (few/no reviews)"]
          + [f"Nothing found for {LABEL[c]}" for c, v in counts.items() if v == 0])
    return uniq


def prefilter(p: UserProfile, places: list[Place], trace) -> list[Place]:
    """Cheap filter before enrichment: hard diet constraint where known, then keep top-N per category."""
    kept, dropped_veg = [], 0
    by_cat: dict[str, list[Place]] = {}
    for pl in places:
        if p.vegetarian and TAXONOMY[pl.category]["meal"] and pl.veg_friendly is False:
            dropped_veg += 1
            continue
        by_cat.setdefault(pl.category, []).append(pl)

    def rank(pl: Place) -> float:
        q = 0.0
        if pl.rating:
            q += (pl.rating - 3.5) * 2 + math.log10((pl.reviews or 1) + 1)
        q += 0.6 * bool(pl.sat_hours) + 0.4 * bool(pl.cuisine) + 0.8 * bool(pl.veg_friendly) + 3 * pl.web_mentions
        if p.avoid_crowds and pl.reviews and pl.reviews > 3000:
            q -= 1.2 * math.log10(pl.reviews / 3000 + 1) * 2  # mega-popular spots are the crowded ones
        return q - 0.1 * pl.distance_km
    for cat, lst in by_cat.items():
        ranked = sorted(lst, key=rank, reverse=True)
        kept += ranked[:PER_CATEGORY_KEEP] + [x for x in ranked[PER_CATEGORY_KEEP:] if x.web_mentions]
    trace("Pre-filter", f"{len(places)} places", f"Kept {len(kept)} for enrichment (top {PER_CATEGORY_KEEP}/genre by quality & proximity)",
          [f"Removed {dropped_veg} places tagged non-vegetarian"] if dropped_veg else [])
    return kept


# ======================= enrichment =======================
class _Enriched(BaseModel):
    id: str
    vibe_tags: list[str] = Field(description="2-4 of: quiet, cozy, lively, scenic, artsy, family, romantic, heritage, trendy, green, budget, upscale, local-favourite, touristy")
    energy: Literal["low", "medium", "high"]
    duration_min: int = Field(description="realistic minutes a visitor spends")
    cost_min: int = Field(description="INR per person, low end, incl. typical order/ticket")
    cost_max: int
    veg_friendly: Optional[bool] = Field(description="true if good vegetarian options likely, false if unlikely (e.g. meat-focused), null if non-food")
    indoor: bool
    crowd_level: float = Field(description="0-1 typical Saturday crowding")
    one_liner: str = Field(description="≤15 words describing what it is, factual; say 'unknown' rather than invent")


class _EnrichBatch(BaseModel):
    places: list[_Enriched]


ENRICH_SYS = """You are a well-travelled local guide for Indian cities. For each place, estimate planning attributes from its name, category, cuisine, rating and any tags.
Be realistic and conservative: if you don't recognise a place, infer from category and name (e.g. 'Sri Krishna Sagar' → South Indian vegetarian; 'Barbeque Nation' → meat-heavy but has veg options → true; 'Meghana Biryani' → biryani-focused, vegetarian options weak → false).
Costs are per person in INR for {city} for a typical visit (food: a normal order; attractions: entry fee; parks: usually 0-30).
Durations: cafe 45-75, meal 50-80, park walk 40-75, museum 60-120, live music 90-150."""


def _prior(pl: Place):
    t = TAXONOMY[pl.category]
    lo, hi = t["cost"]
    if pl.price_level is not None and t["meal"]:
        lo, hi = {0: (0, 100), 1: (100, 300), 2: (300, 700), 3: (700, 1500), 4: (1500, 3000)}[pl.price_level]
    pl.cost_min, pl.cost_max = lo, hi
    pl.energy, pl.duration_min, pl.indoor = t["energy"], t["dur"], t["indoor"]
    pl.crowd_level = 0.5
    if pl.reviews:
        pl.crowd_level = min(0.95, 0.2 + 0.15 * math.log10(pl.reviews + 1))
    if pl.category == "market":
        pl.crowd_level = max(pl.crowd_level, 0.8)


def enrich(p: UserProfile, places: list[Place], trace) -> list[Place]:
    for pl in places:
        _prior(pl)
    todo = []
    cached = 0
    for pl in places:
        c = cache_get("enrich", f"{pl.id}|{p.city.lower()}")
        if c:
            _apply(pl, _Enriched(**c)); cached += 1
        else:
            todo.append(pl)

    def run(batch: list[Place]):
        lines = [{"id": x.id, "name": x.name, "category": x.category, "cuisine": x.cuisine, "rating": x.rating,
                  "reviews": x.reviews, "price_level": x.price_level, "summary": x.summary, "address": x.address} for x in batch]
        try:
            res = llm_parse(ENRICH_SYS.format(city=p.city), f"City: {p.city}\nPlaces: {lines}", _EnrichBatch)
            return res.places
        except Exception as e:
            log.warning("Enrichment batch of %d failed: %s", len(batch), str(e)[:200])
            return []

    batches = [todo[i:i + ENRICH_BATCH] for i in range(0, len(todo), ENRICH_BATCH)]
    got = 0
    with ThreadPoolExecutor(4) as ex:
        for res in [f.result() for f in [submit(ex, run, b) for b in batches]]:
            for e in res:
                pl = next((x for x in todo if x.id == e.id), None)
                if pl:
                    _apply(pl, e); got += 1
                    cache_set("enrich", f"{pl.id}|{p.city.lower()}", e.model_dump())
    trace("Place Enricher", f"{len(places)} places",
          f"Enriched {got} via LLM, {cached} from cache, {len(places) - got - cached} with category priors",
          ["Estimated vibe, energy, duration, per-person cost, veg-friendliness, indoor/outdoor, crowd level"])
    return places


def _apply(pl: Place, e: _Enriched):
    pl.vibe_tags = e.vibe_tags
    pl.energy = e.energy
    pl.duration_min = max(20, min(180, e.duration_min))
    if pl.price_level is None or not TAXONOMY[pl.category]["meal"]:
        cap = max(300, TAXONOMY[pl.category]["cost"][1] * 3)  # LLM sometimes returns membership/annual fees
        lo, hi = max(0, min(e.cost_min, cap)), max(0, min(e.cost_max, cap))
        pl.cost_min, pl.cost_max = min(lo, hi), max(lo, hi)
    if pl.veg_friendly is None:  # never override explicit source data
        pl.veg_friendly = e.veg_friendly
    pl.indoor = e.indoor
    pl.crowd_level = 0.5 * pl.crowd_level + 0.5 * max(0.0, min(1.0, e.crowd_level))
    if not pl.summary and e.one_liner.lower() != "unknown":
        pl.summary = e.one_liner
    pl.enriched_by = "llm"
