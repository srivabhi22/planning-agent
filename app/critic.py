"""M10 — Critic (local-friend review that can request swaps) + Explainer (reasons, trade-offs, tips)."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .models import DayContext, Plan, UserProfile
from .util import llm_parse


def plan_brief(plan: Plan) -> list[dict]:
    out = []
    for i, s in enumerate(plan.stops):
        pl = s.place
        out.append({
            "index": i, "slot": s.slot_type, "time": f"{s.start:%H:%M}-{s.depart:%H:%M}",
            "place": pl.name, "place_id": pl.id, "category": pl.category, "cuisine": pl.cuisine,
            "rating": pl.rating, "reviews": pl.reviews, "veg_friendly": pl.veg_friendly, "indoor": pl.indoor,
            "energy": pl.energy, "vibe": pl.vibe_tags, "cost_per_group": f"{s.cost_min:.0f}-{s.cost_max:.0f}",
            "distance_from_start_km": round(pl.distance_km, 1), "summary": pl.summary,
            "travel_in": f"{s.leg_in.mode} {s.leg_in.minutes} min, {s.leg_in.km} km" if s.leg_in else None,
            "web_recommendation": pl.web_note, "score_breakdown": s.breakdown,
        })
    return out


# ---------------- critic ----------------
class _Swap(BaseModel):
    stop_index: int
    action: Literal["replace", "remove", "set_duration"]
    minutes: Optional[int] = Field(None, description="only for set_duration")
    reason: str




CRITIC_SYS = """You are a sharp, practical local friend reviewing a Saturday plan before your friend follows it.
Check realism, not perfection: Is it doable for this mood/energy? Are durations realistic (e.g. 30 min is too short for a proper meal; 2h at a small garden is too long)?
Is a place plausibly wrong for a hard constraint (e.g. a meat-focused place for a vegetarian; a known-busy spot for someone avoiding crowds)?
Is an area unpleasant/unsafe late at night? Is the plan too rushed or zig-zagging? Is an outdoor stop scheduled during rain/heat?
Request swaps ONLY for real problems (max 2). Use 'replace' to get a different place for that slot, 'remove' to drop it, 'set_duration' to fix a duration.
Never remove or replace the only stop that covers one of the person's stated interests unless it is truly unsuitable — the plan must still reflect what they asked for.
If the plan is sensible, say verdict=good with no swaps. Never nitpick style."""


# ---------------- explainer ----------------
class _StopText(BaseModel):
    index: int
    reason: str = Field(description="ONE short sentence, max 20 words, plain friendly language: why this stop, for them, at this time")
    tip: str = Field(description="max 12 words practical tip, or empty")


class _Explanation(BaseModel):
    title: str = Field(description="≤7 words")
    summary: str = Field(description="ONE sentence, max 30 words, the shape of the day")
    stops: list[_StopText]
    tradeoffs: list[str] = Field(description="0-3 items, each max 15 words, only real compromises (e.g. 'Rain 4–6 PM, so the lake walk is swapped for a bookstore.'). Empty if none.")
    tips: list[str] = Field(description="0-2 items, max 12 words each")


EXPLAIN_SYS = """You write the final Saturday plan for a friend. Short, crisp, human — like a text message from a local friend. No fluff, no marketing words, no invented facts.
Only state facts about a place that are in the data (name, category, cuisine, rating, vibe, summary); phrase inferences as likely ("usually quiet in the afternoon").
Each reason must connect the stop to the person: their mood/energy, interests, hard constraints, the weather at that hour, or the timing (sunset, meal time).
Mention honest trade-offs using the planner's notes as the source of truth — never claim a place type doesn't exist nearby; say why it was left out (e.g. rain, daylight, time)."""


def explain(p: UserProfile, ctx: DayContext, plan: Plan, trace_notes: list[str]) -> Plan:
    user = (f"Person: mood='{p.mood}', interests {p.interests}, hard constraints {p.hard_constraints}, soft {p.soft_preferences}, "
            f"budget {'₹' + str(p.budget) if p.budget else 'not specified'} for {p.group_size} people.\n"
            f"Saturday {ctx.date}, {ctx.place_label}. Weather: {ctx.weather_summary}. Sunset {ctx.sunset:%H:%M}.\n"
            f"Plan: {plan_brief(plan)}\nCost: ₹{plan.cost_min:.0f}-{plan.cost_max:.0f}; notes {plan.cost_notes}\n"
            f"Planner penalties/trade-offs detected: {plan.penalties}\nOther agent notes: {trace_notes}")
    try:
        ex = llm_parse(EXPLAIN_SYS, user, _Explanation, effort="medium")
        apply_explanation(plan, ex)
    except Exception as e:
        plan.title = "Your Saturday plan"
        plan.summary = f"Explainer unavailable ({type(e).__name__}); showing the computed plan."
        for s in plan.stops:
            top = sorted(s.breakdown.items(), key=lambda kv: kv[1], reverse=True)[:2]
            s.reason = f"Best-scoring {s.place.category.replace('_', ' ')} for this slot (strongest on {', '.join(k for k, _ in top)})."
    return plan


# ---------------- critic (+ explanation in the same call when the plan is approved) ----------------
class _Critique(BaseModel):
    verdict: Literal["good", "revise"]
    issues: list[str]
    swaps: list[_Swap] = Field(description="at most 2, only for real problems")
    explanation: Optional[_Explanation] = Field(None, description="ONLY when verdict is 'good': the final write-up (see writing rules). null when revising.")


def critique(p: UserProfile, ctx: DayContext, plan: Plan, trace_notes: list[str] | None = None, write: bool = True) -> _Critique:
    """Review the plan; if it's good (and write=True), also return the final explanation — saves a separate call."""
    user = (f"Person: mood='{p.mood}', energy curve {p.energy_curve}, interests {p.interests}, hard constraints {p.hard_constraints}, "
            f"soft {p.soft_preferences}, budget {'₹' + str(p.budget) if p.budget else 'not specified'} for {p.group_size} people, transport cab/auto/metro.\n"
            f"Saturday {ctx.date} in {ctx.place_label}. Weather: {ctx.weather_summary}. Sunset {ctx.sunset:%H:%M}.\n"
            f"Window {ctx.start:%H:%M}-{ctx.end:%H:%M}. Estimated cost ₹{plan.cost_min:.0f}-{plan.cost_max:.0f}; notes {plan.cost_notes}\n"
            f"Planner trade-offs detected: {plan.penalties}\nOther agent notes: {trace_notes or []}\n"
            f"Plan: {plan_brief(plan)}")
    system = CRITIC_SYS + ("\n\nIf (and only if) your verdict is 'good', also fill `explanation` following these writing rules:\n" + EXPLAIN_SYS
                           if write else "\nAlways leave `explanation` null.")
    return llm_parse(system, user, _Critique)


def apply_explanation(plan: Plan, ex: _Explanation) -> Plan:
    plan.title, plan.summary = ex.title, ex.summary
    plan.tradeoffs, plan.tips = ex.tradeoffs, ex.tips
    by = {x.index: x for x in ex.stops}
    for i, s in enumerate(plan.stops):
        if i in by:
            s.reason, s.tip = by[i].reason, by[i].tip
    return plan
