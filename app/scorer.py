"""M6 — Scorer: hard filters + weighted, profile-adaptive score per slot."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from .context import outdoor_ok
from .genres import MEAL_OPTIONS, TAXONOMY
from .util import run_state
from .models import ENERGY_NUM, Candidate, DayContext, Place, Slot, UserProfile

TOP_K = 8

# crowd multiplier by hour on a Saturday, per broad category family
_CROWD_CURVE = {
    "food":    {8: .5, 9: .6, 10: .5, 12: .7, 13: .9, 14: .7, 16: .5, 18: .6, 19: .8, 20: 1.0, 21: .9, 22: .6},
    "outdoor": {6: .6, 7: .7, 8: .6, 10: .4, 12: .3, 14: .3, 16: .6, 17: .9, 18: 1.0, 19: .7, 20: .4},
    "indoor":  {10: .4, 12: .6, 14: .8, 16: .9, 18: .8, 20: .6},
    "night":   {18: .4, 19: .6, 20: .8, 21: 1.0, 22: 1.0, 23: .8},
}


def _curve_val(curve: dict, hour: int) -> float:
    ks = sorted(curve)
    lo = max([k for k in ks if k <= hour], default=ks[0])
    return curve[lo]


def crowd_at(pl: Place, when: datetime) -> float:
    t = TAXONOMY[pl.category]
    fam = "night" if pl.category in ("live_music", "bar_pub") else "food" if t["meal"] else ("indoor" if pl.indoor else "outdoor")
    return min(1.0, pl.crowd_level * 1.15 * _curve_val(_CROWD_CURVE[fam], when.hour) + 0.05)  # 1.15 = Saturday factor


def open_minutes(pl: Place, when: datetime) -> int | None:
    """Minutes remaining open from `when`; 0 if closed; None if hours unknown (use category defaults)."""
    m = when.hour * 60 + when.minute
    iv = pl.sat_hours
    if iv is None:
        a, b = TAXONOMY[pl.category]["hours"]
        iv = [(a * 60, b * 60)]
    for a, b in iv:
        if a <= m < b:
            return b - m
    return 0


def opens_at(pl: Place, when: datetime) -> int | None:
    """Minute-of-day the place next opens after `when` (same day), else None."""
    m = when.hour * 60 + when.minute
    iv = pl.sat_hours if pl.sat_hours is not None else [(TAXONOMY[pl.category]["hours"][0] * 60, TAXONOMY[pl.category]["hours"][1] * 60)]
    nxt = [a for a, b in iv if a > m]
    return min(nxt) if nxt else None


def bayes_quality(pl: Place) -> float:
    if pl.rating is None:
        return 0.5  # unknown → neutral
    m, C = 60, 4.0
    v = pl.reviews or 0
    b = (v * pl.rating + m * C) / (v + m)
    return max(0.0, min(1.0, (b - 3.6) / 1.1))


def weights(p: UserProfile) -> dict[str, float]:
    w = dict(interest=1.4, mood=1.1, quality=1.2, gem=0.35, time=0.8, weather=1.0, budget=0.7, crowd=0.4, distance=0.15, web=1.0)
    if p.budget is None:
        w["budget"] = 0.2  # no budget given → cost only a mild tiebreaker
    elif p.budget / p.group_size < 1200:
        w["budget"] = 1.4
    if p.avoid_crowds:
        w["crowd"] = 1.4
    if p.energy_curve.count("low") >= 3:
        w["mood"] = 1.5
        w["distance"] = 0.5  # tired people dislike long rides
    if any("hidden" in s.lower() or "offbeat" in s.lower() for s in p.soft_preferences):
        w["gem"] = 0.9
    return w


def score_slot(p: UserProfile, ctx: DayContext, slot: Slot, places: list[Place], n_slots: int) -> tuple[list[Candidate], dict]:
    W = weights(p)
    HOLIDAY = run_state().holiday
    interests = {i.lower() for i in p.interests}
    prefs = {s.lower() for s in p.soft_preferences} | ({"quiet"} if p.avoid_crowds else set())
    per_person = p.budget / p.group_size if p.budget else 4000  # no budget → treat ~₹4000/person as "expensive"
    budget_share = per_person / max(1, n_slots) * (1.6 if slot.is_meal else 1.2)
    stats = dict(considered=0, closed=0, diet=0, cost=0, crowd=0)
    out, crowded = [], []
    for pl in places:
        if pl.category not in slot.categories:
            continue
        stats["considered"] += 1
        # ---------- Stage A: hard filters ----------
        mid_visit = slot.start + timedelta(minutes=15)
        if (open_minutes(pl, slot.start) or 0) < 30 and (open_minutes(pl, slot.latest) or 0) < 30 and not _opens_in_window(pl, slot):
            stats["closed"] += 1
            continue
        if p.vegetarian and TAXONOMY[pl.category]["meal"] and pl.veg_friendly is False:
            stats["diet"] += 1
            continue
        if any("alcohol" in c.lower() for c in p.hard_constraints) and pl.category == "bar_pub":
            continue
        if HOLIDAY and HOLIDAY.dry_day and pl.category == "bar_pub":
            continue  # dry day: no alcohol served
        if p.budget and pl.cost_min > 0.55 * p.budget / p.group_size:
            stats["cost"] += 1
            continue
        crowd = crowd_at(pl, mid_visit)
        too_crowded = p.avoid_crowds and crowd > 0.9
        # ---------- Stage B: weighted score ----------
        t = TAXONOMY[pl.category]
        b = {}
        tag_hit = len(prefs & set(pl.vibe_tags)) * 0.25
        b["interest"] = min(1.0, (0.75 if set(t["interests"]) & interests else 0.35) + tag_hit)
        b["mood"] = 1 - abs(ENERGY_NUM[pl.energy] - ENERGY_NUM[slot.target_energy]) / 2
        b["quality"] = bayes_quality(pl)
        b["gem"] = 1.0 if (pl.rating or 0) >= 4.4 and 25 <= (pl.reviews or 0) <= 600 else 0.0
        h = slot.start.hour
        b["time"] = 1.0 if t["hours"][0] <= h < t["hours"][1] else 0.4
        if slot.type == "sunset" and not pl.indoor and pl.category in ("viewpoint", "garden_lake", "park"):
            b["time"] = 1.2
        if slot.type == "breakfast" and pl.category in ("breakfast", "cafe", "bakery_dessert"):
            b["time"] = 1.1
        ok, _ = outdoor_ok(ctx, h)
        b["weather"] = 1.0 if pl.indoor or ok else 0.0
        b["budget"] = max(0.0, 1 - pl.cost_mid / max(1, budget_share)) if pl.cost_mid else 1.0
        b["crowd"] = -crowd
        b["distance"] = -min(1.0, pl.distance_km / 15)
        if p.vegetarian and TAXONOMY[pl.category]["meal"]:
            b["interest"] += 0.15 if pl.veg_friendly else -0.1  # unknown veg status is a small risk
        if slot.is_meal and pl.category not in MEAL_OPTIONS[slot.type]:
            b["meal_fit"] = -0.15 if pl.category in ("live_music", "bar_pub") else -1.0  # music venues/pubs do serve food  # a bar/music venue/cafe can fill a meal slot only if it clearly wins otherwise
        b["web"] = min(1.0, 0.6 * pl.web_mentions)  # recommended by blogs/lists/reddit
        sc = sum(W.get(k, 2.0) * v for k, v in b.items())
        if HOLIDAY and HOLIDAY.is_holiday:
            if pl.category in HOLIDAY.likely_closed and pl.hours_source != "date":
                b["holiday_risk"] = -1.0  # hours not confirmed for this date and this kind of place often closes
            if HOLIDAY.busier:
                b["crowd"] *= 1.25
            sc = sum(W.get(k, 2.0) * v for k, v in b.items())
        cand = Candidate(place=pl, score=round(sc, 3), breakdown={k: round(v, 2) for k, v in b.items()})
        if too_crowded:
            stats["crowd"] += 1
            crowded.append(cand)
        else:
            out.append(cand)
    # crowd is a hard filter only while alternatives exist: at Saturday peak (e.g. 8:30 PM dinner) every
    # popular place looks busy — then keep the least-crowded few (already penalised) rather than an empty slot
    if len(out) < 3 and crowded:
        crowded.sort(key=lambda c: (c.breakdown["crowd"], c.score), reverse=True)
        keep = crowded[:3 - len(out)]
        out += keep
        stats["relaxed"] = len(keep)
    out.sort(key=lambda c: c.score, reverse=True)
    return out[:TOP_K], stats


def _opens_in_window(pl: Place, slot: Slot) -> bool:
    o = opens_at(pl, slot.earliest)
    return o is not None and o <= slot.latest.hour * 60 + slot.latest.minute


def score_all(p: UserProfile, ctx: DayContext, slots: list[Slot], places: list[Place], trace, banned: set[str] = frozenset()):
    res, lines, removed = {}, [], {"closed": 0, "diet": 0, "cost": 0, "crowd": 0}
    pool = [x for x in places if x.id not in banned]
    for s in slots:
        cands, st = score_slot(p, ctx, s, pool, len(slots))
        res[s.id] = cands
        dec = []
        if st["closed"]: dec.append(f"{st['closed']} closed at that time")
        if st["diet"]: dec.append(f"{st['diet']} not vegetarian-friendly")
        if st["cost"]: dec.append(f"{st['cost']} too expensive for the budget")
        if st["crowd"]: dec.append(f"{st['crowd']} likely too crowded at {s.start:%I %p}")
        if st.get("relaxed"): dec.append(f"Everything is busy at this hour — kept the {st['relaxed']} least-crowded option(s)")
        for k in removed:
            removed[k] += st[k]
        top = ", ".join(c.place.name for c in cands[:3])
        lines.append(f"{s.start:%I:%M %p} {s.type}: {top or 'no suitable place'}" + (f" ({'; '.join(dec)})" if dec else ""))
    labels = {"closed": "closed at that time", "diet": "didn't fit diet", "cost": "over budget", "crowd": "too crowded"}
    summary = ", ".join(f"{v} {labels[k]}" for k, v in removed.items() if v)
    trace("Scorer", f"{len(slots)} time slots, {len(pool)} places",
          f"Ranked places for every slot" + (f" — filtered out: {summary}" if summary else ""), lines)
    return res
