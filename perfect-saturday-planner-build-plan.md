# Perfect Saturday Planner: Build Plan

## Goal

Given a user's city, budget, time, mood, interests, and constraints (via form or free text), produce a **realistic, specific, followable Saturday plan** using real place data, real weather, and traffic-aware travel times. Every stop comes with a reason it fits the user, and a trace shows what the agent did.

## Core design principle

**The LLM handles judgment; code handles math.** The LLM interprets the user, picks place genres, tags vibes, reviews the plan, and writes explanations. Deterministic code handles time windows, distances, travel time, costs, scoring, and sequencing. Never ask the LLM to add up money or minutes.

## Pipeline overview

```
User input
  → [M1] Input Parser              → UserProfile
  → [M2] Context Builder           → DayContext (location, time window, weather, sunset)
  → [M3] Genre Planner (LLM)       → SearchIntent (categories per slot)
  → [M4] Place Retriever           → raw candidate places
  → [M5] Place Enricher            → enriched places (ratings, cost, vibe, duration...)
  → [M6] Scorer                    → ranked candidates per slot
  → [M7] Travel Time Service       → traffic-aware travel matrix
  → [M8] Sequencer                 → best feasible itineraries
  → [M9] Cost Estimator            → cost breakdown
  → [M10] Critic + Explainer (LLM) → final plan with reasons
  → [M11] Orchestrator + Trace + UI
```

---

## M0. Shared data contracts (build first)

Define these shared structures so all modules agree:

- **UserProfile:** city/area, budget, time window or duration, mood, interests, hard constraints (e.g. vegetarian, avoid crowds), soft preferences, and assumptions made.
- **DayContext:** coordinates, planning start/end, hourly weather, sunrise/sunset, current time.
- **Place:** id, name, coordinates, category, opening hours, rating, review count, price level, diet tags, plus enriched fields (vibe tags, energy level, typical duration, estimated cost per person, crowd estimate by hour, indoor/outdoor).
- **Slot:** type (breakfast, morning activity, lunch, afternoon, sunset, dinner, night), time range, target energy level, and whether it's a meal.
- **Plan:** ordered stops with arrival/departure times, travel legs, cost per stop, total cost, total time, and a reason per stop.
- **TraceEvent:** step name, short input summary, short output summary, decisions made.

---

## M1. Input Parser

**Purpose:** convert form data or free text into a UserProfile.

**Intuition:**
- Separate **hard constraints** (never broken: diet, crowd avoidance, accessibility) from **soft preferences** (interests that can be traded off).
- Convert mood into a **target energy curve** for the day. "Tired but wants fun" becomes low → low → medium → low. "Energetic" becomes medium → high → high → medium.
- Record any assumption made explicitly ("no area given, using city centre").

**Tools:** LLM with structured output. Form input can skip the LLM and map directly.

---

## M2. Context Builder

**Purpose:** establish the real-world context of the day.

**Intuition:**
- **Time window:** the default Saturday runs roughly 7 AM to 11:30 PM. If today is Saturday, start = current time + a 30–45 min prep/travel buffer, rounded up to the next half hour. If the user gives a duration ("4 hours"), choose the best start time within the day for their interests (e.g. late afternoon for walks and sunset, evening for music).
- **Weather awareness:** hourly rain probability, temperature, and UV. Outdoor activities should avoid rain hours and harsh midday heat (very relevant in Indian cities). Heavy rain across the day should tilt the plan toward indoor places.
- **Sunset time** is used to anchor scenic or walking stops.
- **Air quality** (optional but useful in Indian cities): poor AQI should reduce long outdoor stops.

**APIs:**
- **Nominatim (OSM):** geocode the city or area to coordinates. Free, no key.
- **Open-Meteo Forecast API:** hourly weather, sunrise/sunset. Free, no key.
- **Open-Meteo Air Quality API:** hourly AQI. Free, no key.

---

## M3. Genre Planner (LLM)

**Purpose:** decide *what kinds* of places to search for, per slot.

**Intuition:**
- Use a **fixed taxonomy** mapping interests to OSM tags (e.g. food → restaurant, cafe, bakery, fast food; walks → park, garden, lake, promenade, heritage area; music → music venue, bar, arts centre; culture → museum, gallery, theatre). The LLM chooses from this taxonomy instead of inventing tags, which keeps retrieval reliable.
- Build the **day skeleton** from the time window: which slots exist and what each needs (a meal slot needs food; an activity slot needs interest-matching places).
- Adjust genres for mood and weather: tired means lower-effort genres, rain means indoor genres, sunset slots mean scenic outdoor places.
- Output a SearchIntent: for each slot, the categories to fetch and the target energy level.

**Tools:** LLM with structured output plus the taxonomy as context.

---

## M4. Place Retriever

**Purpose:** fetch real candidate places for each category near the user.

**Intuition:**
- Search within a sensible radius of the user's area (e.g. 5–8 km in big cities). Prefer a compact area, since a plan spread across the city is impractical.
- Deduplicate and drop places missing names or coordinates.
- Keep candidate lists reasonably sized per category, since enrichment costs calls.

**APIs:**
- **Overpass API (OpenStreetMap):** place discovery by tag and radius, including opening hours, cuisine, and diet tags where available. Free, no key.
- **Google Places API (New), Nearby/Text Search:** optional alternative or supplement with much better coverage in Indian cities. Free monthly usage, card required.

---

## M5. Place Enricher

**Purpose:** add the attributes planning needs that raw data lacks.

**Intuition:**
- **Quality:** rating plus review count. Raw ratings mislead, since 4.9 from 8 reviews isn't better than 4.5 from 3,000.
- **Cost:** price level plus category priors for the city, refined by the LLM, stored as a per-person range.
- **Vibe tags:** quiet, cozy, lively, scenic, artsy, and similar.
- **Energy level:** low, medium, or high effort.
- **Typical duration:** how long people realistically spend there.
- **Crowd estimate by hour:** heuristic of category × hour × Saturday factor, scaled by review count as a popularity proxy.
- **Indoor/outdoor**, so weather can be applied.
- **Diet compatibility:** use OSM or Google tags where present, and let the LLM infer from cuisine and name when missing (e.g. a South Indian vegetarian restaurant).
- Enrich only the top candidates after a cheap pre-filter, and **cache everything**, since place attributes don't change daily.

**APIs:**
- **Google Places API (New), Place Details:** rating, review count, price level, opening hours, and some dining attributes like vegetarian options. Request only the fields you need.
- **Web search API** (Tavily or Serper free tier): fallback source for ratings and review sentiment, with the LLM extracting the information.
- **LLM:** vibe, energy, duration, crowd, and cost estimates.

---

## M6. Scorer (recommendation)

**Purpose:** rank candidates for each slot.

**Stage A, hard filters:** open during the slot, hard constraints satisfied, per-stop cost within a reasonable share of the budget, within radius.

**Stage B, weighted score:**
- **Interest match:** similarity between the user's interests and the place's tags/description (embeddings work well here).
- **Mood fit:** place energy versus the slot's target energy.
- **Quality:** Bayesian-averaged rating.
- **Hidden-gem bonus:** high rating with relatively low review count, which surfaces good places users may not know about.
- **Time fit:** breakfast places in the morning, scenic spots near sunset, music venues in the evening.
- **Weather fit:** outdoor places penalised during rain, heat, or poor AQI.
- **Budget fit.**
- **Crowd penalty,** heavier if the user avoids crowds.

Weights adjust to the profile (a tight budget increases budget weight, "avoid crowds" increases the crowd penalty).

---

## M7. Travel Time Service

**Purpose:** realistic travel time between places at the time of travel.

**Intuition:**
- Traffic in Indian cities changes travel time dramatically. A 6 km trip can take 15 minutes at 9 AM on a Saturday and 45 minutes at 7 PM. Travel time must depend on **departure time**.
- Compute a travel-time matrix among shortlisted places, not all places.
- Prefer walking between nearby stops (roughly under 1 km) when weather allows. It's free, pleasant, and avoids traffic.
- Include a small buffer per leg for parking, booking a cab, or finding the entrance.

**APIs (best to fallback):**
- **Google Routes API:** traffic-aware durations for a given departure time. Best quality for Indian cities, free monthly usage (card required).
- **TomTom Routing API:** traffic-aware routing on a free tier. Good alternative to Google.
- **OpenRouteService:** free key, driving and walking, but no live traffic.
- **Simple fallback:** road distance divided by an hour-based city speed profile (e.g. slower on Saturday evenings).

---

## M8. Sequencer (the core planning logic)

**Purpose:** build the best feasible itinerary through the day.

**Framing:** an **orienteering problem with time windows**: maximise total plan score subject to the time window, opening hours, travel times, and budget.

**Approach:** beam search over slots. Extend partial plans stop by stop, keep the top K, and discard any partial plan that breaks time, budget, or opening hours. Candidate counts per slot are small, so this stays fast.

**Human-rhythm rules** (encode as constraints or penalties):
- **Eat at meal times.** Meal stops go near real meal hours, and food should come after about 2.5–3 hours of continuous activity.
- **No back-to-back heavy meals.** A cafe or dessert stop can follow a meal later, not straight after.
- **Energy flow follows the mood curve.** No two high-effort stops in a row for a tired user; the day winds down at the end.
- **Light activity after a heavy meal,** such as a short walk rather than a trek.
- **Outdoor at the right time:** morning or late afternoon for walks, sunset spot at sunset, indoor during midday heat or rain.
- **Geographic compactness:** stay within one or two neighbourhoods; penalise zig-zagging across the city.
- **Diversity:** avoid repeating the same category consecutively.
- **Slack:** don't pack every minute. Leave breathing room so the plan survives real life.
- **Ending well:** end at a calm or memorable stop, reasonably close to the start area.
- **Duration realism:** use enriched typical durations, not arbitrary ones.

Output the top 2–3 complete plans. Show the best one; keep the rest as alternatives.

---

## M9. Cost Estimator

**Purpose:** all-in cost breakdown.

**Intuition:**
- Include food, entry fees, and travel (cab/auto cost estimated from distance using typical per-km city rates).
- Show ranges, not false precision.
- Target under budget with a buffer (e.g. plan to around 85% of the budget).
- Explicitly flag when a stop is slightly over its share but fits much better (the trade-off the assignment wants explained).

---

## M10. Critic + Explainer (LLM)

**Critic purpose:** review the chosen plan like a local friend would: "Is this realistic for someone tired? Is 30 minutes enough here? Is this area pleasant at night? Is this place actually good for vegetarians?" It can request specific swaps, which go back to M6–M8 for re-ranking and re-sequencing. Limit to one or two rounds.

**Explainer purpose:** write the final plan:
- Timeline with times, place, duration, cost, and travel legs
- One or two lines per stop explaining **why it fits this user** (mood, interest, constraint, weather, timing)
- Trade-off notes where the plan compromised
- Assumptions made
- Map links per stop

---

## M11. Orchestrator, Trace, UI

**Orchestrator:** runs M1 → M10 in order, passing shared state, and emits a TraceEvent at every step.

**Trace:** a human-readable log, e.g. "Found 42 cafes near Indiranagar," "Filtered to 11 vegetarian-friendly and quiet," "Rain expected 3–5 PM, moved the lake walk to 5:30 PM," "Traffic adds 20 min to the dinner leg." Stream it live if possible.

**UI:**
- Form plus a free-text box
- Live agent trace panel
- Final plan as a timeline, summary bar (total cost vs budget, total time, weather note), and alternatives
- Hosted at a public URL

---

## API summary

| Purpose | Primary | Alternative / fallback |
|---|---|---|
| Geocoding | Nominatim | Google Geocoding |
| Weather, sunset | Open-Meteo | None needed |
| Air quality | Open-Meteo Air Quality | None needed |
| Place discovery | Overpass (OSM) | Google Places Nearby/Text Search |
| Ratings, price, hours | Google Places Details | Web search API + LLM |
| Traffic-aware travel time | Google Routes | TomTom Routing → OpenRouteService / speed profile |
| Reasoning, tagging, explanation | Any capable LLM | — |

## Suggested build order

1. M0 contracts, then M1 and M2
2. M3 and M4 (see real places for a real city)
3. M5 with caching
4. M6 and M7
5. M8 — the most important module; give it the most care
6. M9 and M10
7. M11: wire everything, trace, UI, deploy

Build and test each module on the sample Bangalore input before wiring:

```json
{
  "city": "Bangalore",
  "budget": 2000,
  "available_time": "4 hours",
  "mood": "tired but wants to do something fun",
  "interests": ["food", "music", "walks"],
  "constraints": ["vegetarian", "avoid crowded places"]
}
```
