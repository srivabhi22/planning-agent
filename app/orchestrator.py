"""M11 — Orchestrator: runs the pipeline with per-request state and emits a TraceEvent at every step.

External calls per plan (cold cache): ~5 LLM calls (parse, local context, genres+web picks, enrichment,
critic+explanation), a handful of web searches, one Google text search per place type/interest, and
Google Routes only for the final plan's legs.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Iterator

from .context import LocationNotFound, build_context
from .cost import estimate_costs
from .critic import apply_explanation, critique, explain
from .genres import build_skeleton, plan_genres
from .localinfo import load_local
from .models import Plan, TraceEvent
from .parser import ServiceBusy, parse_input
from .places import enrich, prefilter, retrieve, web_snippets
from .scorer import score_all
from .sequencer import _interest_cats, retime_with_live_traffic, sequence
from .travel import refine
from .util import log, new_run, run_state

MAX_CRITIC_ROUNDS = 2


class _Timer:
    """Records how long each pipeline stage takes (logged once per run)."""

    def __init__(self):
        self.t, self.name = time.time(), None

    def __call__(self, name):
        now = time.time()
        if self.name:
            run_state().stage_times[self.name] = round(now - self.t, 2)
        self.t, self.name = now, name


def run(form: dict, free_text: str | None, emit) -> dict:
    notes: list[str] = []
    timer = _Timer()

    def trace(step, inp="", out="", decisions=None):
        log.info("[%s] %s | %s", step, inp, out, extra={"event": "step", "stage": step})
        emit({"type": "trace", "event": TraceEvent(step=step, input=inp, output=out, decisions=decisions or []).model_dump()})

    def stage(label):
        timer(label)
        emit({"type": "stage", "label": label})

    # 1. understand the request
    stage("Understanding your preferences")
    try:
        prof, questions = parse_input(form, free_text)
    except ServiceBusy:
        trace("Input Parser", (free_text or "")[:200], "AI service unavailable (quota/outage) — can't read free text")
        return {"error": "The AI service is busy or out of quota right now, so I can't read the free text. "
                         "Fill in the form fields (city, area, people, kinds of places) or try again in a minute."}
    if questions:
        trace("Input Parser", (free_text or str(form))[:200], "Missing or unclear info — asking the user", questions)
        return {"questions": questions}
    trace("Input Parser", (free_text or str(form))[:200],
          f"{prof.area}, {prof.city} · {prof.group_size} people · budget {('₹' + format(prof.budget, ',.0f')) if prof.budget else 'not set'} · energy {' → '.join(prof.energy_curve)}",
          [f"Interests: {', '.join(prof.interests)}", f"Constraints: {', '.join(prof.hard_constraints) or 'none'}",
           "Planning around the kinds of places asked for" if prof.focused else "No specific kinds of places — balanced day"])

    # 2. day context: location, time window, weather; local transport + holiday (one LLM call, cached)
    stage("Checking the weather and your time window")
    try:
        ctx = build_context(prof, trace)
    except LocationNotFound as e:
        trace("Context", str(e), "Couldn't find this location on the map — asking the user")
        return {"questions": [f"I couldn't find the city “{prof.city}” on the map. Could you check the spelling or name a nearby bigger city?"]}
    run_state().target_date = ctx.date
    load_local(prof, ctx, trace)

    # 3. day outline + genres (the same LLM call also picks named places from web search)
    slots = build_skeleton(prof, ctx)
    trace("Day Skeleton", f"{(ctx.end - ctx.start).total_seconds() / 3600:.1f} h window",
          " → ".join(f"{s.start:%I:%M %p} {s.type}{' 🍽' if s.is_meal else ''} ({s.target_energy})" for s in slots))
    stage("Searching the web for local favourites")
    snippets = web_snippets(prof, trace)
    stage("Deciding what kinds of places fit")
    slots, picks = plan_genres(prof, ctx, slots, trace, snippets)

    # 4. places: Google Maps + web picks → pre-filter → enrich
    stage("Finding the best spots on Google Maps")
    places = retrieve(prof, ctx, slots, trace, picks)
    places = prefilter(prof, places, trace)
    stage("Reading up on each place")
    places = enrich(prof, places, trace)
    if not places:
        return {"error": f"I couldn't find suitable places around {prof.area}, {prof.city}. "
                         "Try a nearby, better-known area or broaden the kinds of places."}
    found = {pl.category for pl in places}
    for it in prof.interests:  # honest note for interests that don't exist nearby (e.g. 'beach' in Delhi)
        ic = _interest_cats(it)
        if ic and not ic & found:
            notes.append(f"Couldn't find any {it} within reach of {prof.area}")
            prof.assumptions.append(f"No {it} found near {prof.area} — planned around your other interests")

    # 5. rank → sequence → critic (+explanation) loop; travel uses estimates here, live traffic only at the end
    banned: set[str] = set()
    overrides: dict[str, int] = {}
    plans: list[Plan] = []
    explained = False
    stage("Building the best route")
    for rnd in range(MAX_CRITIC_ROUNDS + 1):
        cands = score_all(prof, ctx, slots, places, trace if rnd == 0 else (lambda *a, **k: None), banned)
        plans = sequence(prof, ctx, slots, cands, overrides)
        if not plans and rnd == 0:
            plans = _relaxed_retry(prof, ctx, slots, trace, stage)
        if not plans:
            return {"error": "I couldn't fit a workable plan into this time window, even after relaxing a few rules. "
                             "Try giving more time, a different start time, or broader kinds of places."}
        plans = [pl.model_copy(deep=True) for pl in plans]  # stops are shared across beam states
        best = plans[0]
        estimate_costs(prof, best)
        trace("Sequencer", f"beam search over {len(slots)} slots" + (f" (revision {rnd})" if rnd else ""),
              "Best: " + " → ".join(f"{s.start:%I:%M} {s.place.name}" for s in best.stops) + f" (score {best.score})",
              [f"{len(plans)} feasible alternatives kept"] + [f"Trade-off: {x}" for x in best.penalties[:5]])
        if rnd == MAX_CRITIC_ROUNDS:
            break
        stage("Double-checking the plan")
        try:
            crit = critique(prof, ctx, best, notes)
        except Exception as e:
            trace("Critic", "", f"Critic unavailable ({type(e).__name__}) — keeping plan")
            break
        trace("Critic", f"round {rnd + 1}", f"Verdict: {crit.verdict}",
              crit.issues + [f"Swap #{w.stop_index}: {w.action} — {w.reason}" for w in crit.swaps])
        if crit.verdict == "good" or not crit.swaps:
            if crit.explanation:
                apply_explanation(best, crit.explanation)
                explained = True
            break
        changed = False
        for w in crit.swaps[:2]:
            if not (0 <= w.stop_index < len(best.stops)):
                continue
            st = best.stops[w.stop_index]
            if w.action == "replace":
                banned.add(st.place.id); changed = True
            elif w.action == "remove":
                banned.add(st.place.id)
                if len(slots) > 2:
                    slots = [s for s in slots if s.id != st.slot_id]
                changed = True
            elif w.action == "set_duration" and w.minutes:
                overrides[st.place.id] = max(20, min(180, w.minutes)); changed = True
            notes.append(f"Critic: {w.reason}")
        if not changed:
            break

    # 6. live traffic / transit for the final plan only
    best = plans[0]
    stage("Checking live traffic")
    tnotes = retime_with_live_traffic(prof, ctx, best, refine)
    src = {s.leg_in.source for s in best.stops if s.leg_in and s.leg_in.mode != "walk"}
    trace("Travel Time", f"{len(best.stops)} legs", f"Sources: {', '.join(src) or 'walking only'}; "
          f"total travel {sum(s.leg_in.minutes for s in best.stops if s.leg_in)} min",
          tnotes + [f"{s.place.name}: {s.leg_in.mode} {s.leg_in.minutes} min ({s.leg_in.km} km)" for s in best.stops if s.leg_in])
    estimate_costs(prof, best)
    for alt in plans[1:]:
        estimate_costs(prof, alt)
    trace("Cost Estimator", "", f"₹{best.cost_min:,.0f} – ₹{best.cost_max:,.0f} (travel ≈ ₹{best.travel_cost:,.0f})"
          + (f" vs budget ₹{prof.budget:,.0f}" if prof.budget else ""), best.cost_notes)

    # 7. explanation (already written by the critic in the common case)
    stage("Finalizing your plan")
    if not explained:
        best = explain(prof, ctx, best, notes + tnotes)
    else:
        best.tradeoffs += [n for n in tnotes if "Traffic" in n][:1]
    st = run_state()
    if st.llm_failures:
        prof.assumptions.append("The AI service was busy for part of this plan — some steps used simpler built-in rules")
    trace("Explainer", "", f"“{best.title}” — reasons written for {len(best.stops)} stops")
    timer(None)
    return {
        "profile": prof.model_dump(),
        "context": {"place": ctx.place_label, "date": ctx.date, "is_today": ctx.is_today,
                    "start": ctx.start.isoformat(), "end": ctx.end.isoformat(), "sunset": ctx.sunset.isoformat(),
                    "weather": ctx.weather_summary, "lat": ctx.lat, "lon": ctx.lon},
        "plan": best.model_dump(mode="json"),
        "alternatives": [a.model_dump(mode="json") for a in plans[1:]],
    }


def _relaxed_retry(prof, ctx, slots, trace, stage):
    """No feasible plan → relax soft rules one step (wider search, crowd/budget limits off, fewer slots) and retry once."""
    stage("Tight fit — relaxing a few rules and retrying")
    relaxed = prof.model_copy(deep=True)
    relaxed.hard_constraints = [c for c in relaxed.hard_constraints if "crowd" not in c.lower()]
    relaxed.budget = None
    st = run_state()
    st.radius_factor = 1.6
    try:
        places = enrich(relaxed, prefilter(relaxed, retrieve(relaxed, ctx, slots, trace), trace), trace)
    finally:
        st.radius_factor = 1.0
    keep = [s for s in slots if s.is_meal] or slots[:1]
    keep += [s for s in slots if not s.is_meal][: max(1, len(slots) // 2)]
    keep.sort(key=lambda s: s.start)
    plans = sequence(relaxed, ctx, keep, score_all(relaxed, ctx, keep, places, trace))
    dropped = [x for x, on in (("crowd limits", len(relaxed.hard_constraints) < len(prof.hard_constraints)),
                               ("budget cap", prof.budget is not None), ("search radius", True)) if on]
    trace("Relaxed retry", f"{len(keep)} slots", f"{'Found' if plans else 'Still no'} feasible plan after relaxing: {', '.join(dropped)}")
    if plans:
        prof.assumptions.append("Tight fit — relaxed " + ", ".join(dropped) + " to make the day work")
    return plans


def stream(form: dict, free_text: str | None) -> Iterator[dict]:
    """Runs the pipeline in a worker thread (with its own run state) and yields events as they happen."""
    q: queue.Queue = queue.Queue()

    def worker():
        st = new_run()  # fresh per-request state + run id in this thread's context
        q.put({"type": "run", "ref": st.run_id})
        t0 = time.time()
        outcome = "ok"
        try:
            out = run(form, free_text, q.put)
            if "questions" in out:
                outcome = "clarify"
                q.put({"type": "clarify", "questions": out["questions"]})
            elif "error" in out:
                outcome = "user_error"
                q.put({"type": "error", "message": out["error"], "ref": st.run_id})
            else:
                q.put({"type": "result", "data": out, "ref": st.run_id})
        except Exception:
            outcome = "crash"
            log.exception("Planning failed", extra={"event": "run", "status": "crash"})
            q.put({"type": "error", "ref": st.run_id,
                   "message": "Something unexpected went wrong while planning. Please try again — "
                              "if it keeps happening, try a slightly different request."})
        finally:
            dt = time.time() - t0
            log.info("run finished: %s in %.1fs · llm=%d (failed %d) · http=%d · stages=%s", outcome, dt,
                     st.llm_calls, st.llm_failures, st.http_calls, st.stage_times,
                     extra={"event": "run", "status": outcome, "duration_ms": int(dt * 1000)})
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()
    while True:
        item = q.get()
        if item is None:
            break
        yield item
