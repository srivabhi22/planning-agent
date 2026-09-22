"""M8 — Sequencer: beam search over slots (orienteering with time windows) + human-rhythm rules."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .genres import TAXONOMY
from .models import ENERGY_NUM, Candidate, DayContext, Plan, Slot, Stop, UserProfile
from .scorer import open_minutes, opens_at
from .travel import estimate

BEAM = 40
BUDGET_TARGET = 0.85  # plan to ~85% of budget
MAX_WAIT = 25  # minutes we'll wait for a place to open


@dataclass
class _State:
    stops: list[Stop] = field(default_factory=list)
    t: datetime = None
    loc: tuple[float, float] = None
    cost: float = 0.0  # mid estimate, group total, incl. travel
    score: float = 0.0
    penalties: list[str] = field(default_factory=list)
    last_meal_end: Optional[datetime] = None
    last_heavy_meal_end: Optional[datetime] = None
    active_since: Optional[datetime] = None  # start of continuous non-food activity
    skipped: int = 0


def _dur(p: UserProfile, c: Candidate, slot: Slot, overrides: dict) -> int:
    pl = c.place
    d = overrides.get(pl.id, pl.duration_min)
    if p.energy_curve.count("low") >= 3 and pl.energy != "low":
        d = int(d * 0.85)  # tired → shorter active stops
    if slot.type == "sunset" and not pl.indoor:
        d = max(d, 45)
    meal_cap = {"breakfast": 50, "lunch": 75, "snack": 45, "dinner": 90}.get(slot.type)
    if meal_cap:
        d = min(d, meal_cap)  # a meal is a meal, not a 2-hour sit-down in the middle of the day
    return max(25, min(d, 150))


def sequence(p: UserProfile, ctx: DayContext, slots: list[Slot], cands: dict[str, list[Candidate]],
             overrides: dict | None = None, n_plans: int = 3) -> list[Plan]:
    overrides = overrides or {}
    start_loc = (ctx.lat, ctx.lon)
    budget = p.budget or float("inf")  # no budget given → never a constraint
    hard_budget = budget  # never exceed; soft target 85%
    tired = p.energy_curve.count("low") >= 3
    beam = [_State(t=ctx.start, loc=start_loc)]

    for si, slot in enumerate(slots):
        nxt: list[_State] = []
        for st in beam:
            # option: skip this slot (keeps plans feasible; penalised, meals more so)
            skip_pen = {"lunch": 3.5, "dinner": 3.5, "breakfast": 1.5, "snack": 0.6}.get(slot.type, 1.2)
            sk = _State(stops=st.stops, t=st.t, loc=st.loc, cost=st.cost, score=st.score - skip_pen,
                        penalties=st.penalties + [f"skipped {slot.type}"], last_meal_end=st.last_meal_end,
                        last_heavy_meal_end=st.last_heavy_meal_end, active_since=st.active_since, skipped=st.skipped + 1)
            nxt.append(sk)
            used = {s.place.id for s in st.stops}
            for c in cands.get(slot.id, []):
                pl = c.place
                if pl.id in used:
                    continue
                # don't leave before the slot is sensible
                depart = max(st.t, slot.earliest - timedelta(minutes=20)) if st.stops else st.t
                leg = estimate(p, ctx, st.loc, (pl.lat, pl.lon), depart)
                arrive = depart + timedelta(minutes=leg.minutes)
                if arrive > slot.latest:
                    continue
                begin = max(arrive, slot.earliest)
                om = open_minutes(pl, begin)
                if not om:
                    o = opens_at(pl, begin)
                    if o is None:
                        continue
                    wait_until = begin.replace(hour=0, minute=0) + timedelta(minutes=o)
                    if (wait_until - begin).total_seconds() / 60 > MAX_WAIT or wait_until > slot.latest:
                        continue
                    begin = wait_until
                    om = open_minutes(pl, begin)
                dur = _dur(p, c, slot, overrides)
                if om is not None and om < dur:
                    if om < 30:
                        continue
                    dur = om - 5  # leave before closing
                end = begin + timedelta(minutes=dur)
                if end > ctx.end:
                    # allow a trimmed last stop if at least 70% of its duration fits
                    dur2 = int((ctx.end - begin).total_seconds() / 60)
                    if dur2 < max(25, 0.7 * dur):
                        continue
                    dur, end = dur2, ctx.end
                stop_cost = pl.cost_mid * p.group_size
                travel_cost = leg.cost  # already for the whole group
                new_cost = st.cost + stop_cost + travel_cost
                if pl.cost_min * p.group_size + st.cost > hard_budget:
                    continue

                sc, pens = c.score, []
                t_cat = TAXONOMY[pl.category]
                is_food = bool(t_cat["meal"])
                heavy = t_cat["meal"] == "meal" or pl.category in ("bar_pub",)
                # --- human-rhythm rules (hard) ---
                if heavy and st.last_heavy_meal_end and (begin - st.last_heavy_meal_end) < timedelta(hours=3):
                    continue  # two proper meals need ≥3h between them
                if is_food and st.last_meal_end and (begin - st.last_meal_end) < timedelta(hours=1.5):
                    continue  # no food stop straight after another food stop
                if is_food and st.stops and TAXONOMY[st.stops[-1].place.category]["meal"] and st.stops[-1].place.category not in ("live_music", "bar_pub"):
                    continue  # consecutive food stops never make sense
                if st.last_heavy_meal_end and (begin - st.last_heavy_meal_end) < timedelta(minutes=60) and pl.energy == "high":
                    sc -= 1.2; pens.append("strenuous right after a big meal")
                if not is_food and st.active_since and (end - st.active_since) > timedelta(hours=3):
                    sc -= 0.9; pens.append("3h+ of activity without a food break")
                if st.stops:
                    prev = st.stops[-1].place
                    if prev.category == pl.category:
                        sc -= 0.8; pens.append(f"repeat {pl.category}")
                    if tired and prev.energy == "high" and pl.energy == "high":
                        sc -= 1.5; pens.append("two high-effort stops in a row")
                    elif ENERGY_NUM[prev.energy] + ENERGY_NUM[pl.energy] >= 4:
                        sc -= 0.5
                # travel & compactness
                # travel: mild per-minute cost; only really long hops hurt (radius is wide on purpose)
                sc -= 0.012 * leg.minutes * (1.6 if tired else 1.0) + 0.0015 * leg.cost / max(1, p.group_size)
                if leg.minutes > 55:
                    sc -= 0.03 * (leg.minutes - 55); pens.append(f"long {leg.minutes}-min transfer")
                idle = (depart - st.t).total_seconds() / 60 + (begin - arrive).total_seconds() / 60
                if idle > 50:
                    sc -= 0.01 * (idle - 50)
                if p.budget and new_cost > BUDGET_TARGET * budget:
                    over = (new_cost - BUDGET_TARGET * budget) / budget
                    sc -= 3 * over; pens.append("eats into the budget buffer")
                if new_cost > budget:
                    continue

                stop = Stop(slot_id=slot.id, slot_type=slot.type, place=pl, arrive=arrive, start=begin, depart=end,
                            leg_in=leg, cost_min=pl.cost_min * p.group_size, cost_max=pl.cost_max * p.group_size,
                            score=c.score, breakdown=c.breakdown)
                ns = _State(stops=st.stops + [stop], t=end, loc=(pl.lat, pl.lon), cost=new_cost, score=st.score + sc,
                            penalties=st.penalties + pens, skipped=st.skipped,
                            last_meal_end=end if is_food else st.last_meal_end,
                            last_heavy_meal_end=end if heavy else st.last_heavy_meal_end,
                            active_since=None if is_food else (st.active_since or begin))
                nxt.append(ns)
        nxt.sort(key=lambda s: s.score, reverse=True)
        beam = _diverse_prune(nxt, BEAM)

    finals = []
    for st in beam:
        if len(st.stops) < min(2, len(slots)):
            continue
        sc, pens = st.score, list(st.penalties)
        last = st.stops[-1].place
        if last.energy == "high":
            sc -= 0.8; pens.append("ends on a high-effort stop")
        if last.vibe_tags and set(last.vibe_tags) & {"scenic", "cozy", "quiet", "romantic"}:
            sc += 0.3  # memorable / calm ending
        # every stated interest should show up somewhere in the day (if we had any candidate for it)
        have = {s.place.category for s in st.stops}
        avail = {c.place.category for cl in cands.values() for c in cl}
        for it in p.interests:
            ic = _interest_cats(it)
            if ic & avail and not ic & have:
                sc -= 3.5  # a stated interest missing from the day is a big miss
                outdoor = all(not TAXONOMY[c]["indoor"] for c in ic)
                pens.append(f"{it} left out — " + ("weather/daylight didn't allow it in this window" if outdoor else "couldn't fit it into the time window"))
        # balanced day: reward covering different kinds of places (soft — never beats hard feasibility)
        if not p.focused:
            from .genres import FAMILY_OF
            fams = {FAMILY_OF.get(s.place.category) for s in st.stops} - {None}
            offered = {FAMILY_OF.get(c.place.category) for cl in cands.values() for c in cl} - {None}
            sc += 1.2 * len(fams)
            for f in offered - fams:
                pens.append(f"no {f} stop — didn't fit naturally")
        back = estimate(p, ctx, st.loc, start_loc, st.t)
        sc -= 0.005 * back.minutes
        window = (ctx.end - ctx.start).total_seconds() / 60
        busy = sum((s.depart - s.start).total_seconds() / 60 + (s.leg_in.minutes if s.leg_in else 0) for s in st.stops)
        if busy > 0.95 * window:
            sc -= 0.8; pens.append("no breathing room")
        if busy < 0.5 * window:
            sc -= 0.6; pens.append("a lot of unused time")
        finals.append((sc, st, pens))
    finals.sort(key=lambda x: x[0], reverse=True)

    plans, sigs = [], []
    for sc, st, pens in finals:
        sig = {s.place.id for s in st.stops}
        if any(len(sig & o) > len(sig) / 2 for o in sigs):
            continue
        sigs.append(sig)
        plans.append(Plan(stops=st.stops, score=round(sc, 2), penalties=sorted(set(pens))))
        if len(plans) >= n_plans:
            break
    # if diversity filter left us short, allow closer variants
    for sc, st, pens in finals:
        if len(plans) >= n_plans:
            break
        if all(p_.signature() != tuple(s.place.id for s in st.stops) for p_ in plans):
            plans.append(Plan(stops=st.stops, score=round(sc, 2), penalties=sorted(set(pens))))
    return plans


def _interest_cats(interest: str) -> set[str]:
    """Taxonomy categories that satisfy a user interest written in their own words ('live music', 'lakes')."""
    from .places import LABEL
    toks = {t.rstrip("s") for t in interest.lower().replace("-", " ").split() if len(t) > 2}
    out = set()
    for cat, meta in TAXONOMY.items():
        words = {w.rstrip("s") for w in (LABEL.get(cat, "") + " " + " ".join(meta["interests"]) + " " + cat.replace("_", " ")).split()}
        if toks & words:
            out.add(cat)
    return out


def _diverse_prune(states: list[_State], k: int) -> list[_State]:
    """Keep top-k but cap near-identical states (same last place) so alternatives survive."""
    out, per_last = [], {}
    for s in states:
        key = s.stops[-1].place.id if s.stops else "_"
        if per_last.get(key, 0) >= 4:
            continue
        per_last[key] = per_last.get(key, 0) + 1
        out.append(s)
        if len(out) >= k:
            break
    return out


def retime_with_live_traffic(p: UserProfile, ctx: DayContext, plan: Plan, refine_fn) -> list[str]:
    """Re-run legs through the live traffic service at their actual departure times and shift the timeline."""
    notes, t, loc = [], ctx.start, (ctx.lat, ctx.lon)
    for i, s in enumerate(plan.stops):
        depart = t if i == 0 else plan.stops[i - 1].depart
        est = estimate(p, ctx, loc, (s.place.lat, s.place.lon), depart)
        leg = refine_fn(p, ctx, loc, (s.place.lat, s.place.lon), est)
        delta = leg.minutes - (s.leg_in.minutes if s.leg_in else 0)
        dur = s.depart - s.start
        arrive = depart + timedelta(minutes=leg.minutes)
        begin = max(arrive, s.start)  # faster leg → keep planned start (opening/slot); slower → push later
        end = begin + dur
        if end > ctx.end:
            end = max(begin + timedelta(minutes=25), ctx.end)
        if leg.traffic_note:
            notes.append(f"{leg.traffic_note} on the leg to {s.place.name}")
        if abs(delta) >= 10 and leg.source != "speed-profile":
            notes.append(f"Live routing changed the leg to {s.place.name} by {delta:+d} min ({leg.source})")
        s.leg_in, s.arrive, s.start, s.depart = leg, arrive, begin, end
        loc = (s.place.lat, s.place.lon)
    return notes
