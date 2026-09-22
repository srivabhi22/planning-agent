"""M9 — Cost Estimator: all-in ranges, budget target, trade-off flags."""
from __future__ import annotations

from .models import Plan, UserProfile
from .sequencer import BUDGET_TARGET


def estimate_costs(p: UserProfile, plan: Plan) -> Plan:
    travel = sum(s.leg_in.cost for s in plan.stops if s.leg_in)
    lo = sum(s.cost_min for s in plan.stops) + travel * 0.85
    hi = sum(s.cost_max for s in plan.stops) + travel * 1.2
    plan.travel_cost = round(travel)
    plan.cost_min, plan.cost_max = round(lo, -1), round(hi, -1)
    plan.travel_minutes = sum(s.leg_in.minutes for s in plan.stops if s.leg_in)
    if plan.stops:
        plan.total_minutes = int((plan.stops[-1].depart - (plan.stops[0].arrive - _td(plan.stops[0].leg_in.minutes))).total_seconds() / 60)
    notes = []
    if not p.budget:
        plan.cost_notes = [f"Expect roughly ₹{lo:,.0f}–₹{hi:,.0f} for {p.group_size} (food, tickets, cabs)."]
        return plan
    target = BUDGET_TARGET * p.budget
    if hi <= target:
        notes.append(f"Even the high estimate (₹{hi:,.0f}) stays under ~85% of your ₹{p.budget:,.0f} budget — buffer for surprises.")
    elif hi <= p.budget:
        notes.append(f"High-end estimate ₹{hi:,.0f} is within budget but uses part of the 15% buffer.")
    else:
        notes.append(f"If you order generously this could reach ₹{hi:,.0f}, above your ₹{p.budget:,.0f} budget — typical spend ≈ ₹{(lo + hi) / 2:,.0f}.")
    share = p.budget / max(1, len(plan.stops))
    for s in plan.stops:
        mid = (s.cost_min + s.cost_max) / 2
        if mid > 1.3 * share and s.score >= 4:
            notes.append(f"{s.place.name} takes more than an even share of the budget (≈₹{mid:,.0f}) but scored much better on fit than cheaper options — kept it and kept other stops cheap.")
    plan.cost_notes = notes
    return plan


def _td(m):
    from datetime import timedelta
    return timedelta(minutes=m)
