# Saturday Planner

An agent that plans a realistic Saturday from a short description: real places (web search + Google Maps),
live weather, traffic-aware and cost-aware travel, meals at the right times, and a short reason for every stop.

## Run locally
```bash
uv venv --python 3.12 && uv pip install -r requirements.txt
cp .env.example .env            # add GEMINI_API_KEY, GOOGLE_MAPS_API_KEY, TAVILY_API_KEY
uv run uvicorn app.main:app --reload --port 8000
```
Open http://localhost:8000

## Pipeline
| Step | Tool(s) | Notes |
|---|---|---|
| Understand request | Gemini | asks when location / people / place types are missing or contradictory |
| Day context | Nominatim, Open-Meteo | time window, hourly rain/storm, sunset |
| Local context | Tavily + Gemini (1 call) | transport options & fares, holiday / dry day — cached per city / date |
| Genres + web picks | Tavily + Gemini (1 call) | kinds of places per slot + named local favourites |
| Places | Google Places | text search per type/interest, date-specific opening hours, type check |
| Enrich | Gemini (1 batch) | vibe, effort, duration, cost for the top few per type — cached |
| Rank + route | code | beam search with meal/energy/variety rules |
| Review + explain | Gemini (1 call) | critic; writes the explanation in the same call when approved |
| Live travel | Google Routes | final plan only: traffic-aware drive vs live transit |

Typical cold run: ~6 LLM calls, ~25 HTTP calls; repeat plans for the same city/date need fewer (disk cache).

## Production features
- **Per-request state** (contextvars) — concurrent users never share data.
- **Structured logs**: `LOG_FORMAT=json` → one JSON line per event with `run_id`, `event` (`access`/`http`/`llm`/`step`/`run`),
  `status`, `duration_ms`. Each run ends with a summary line (outcome, LLM/HTTP call counts, per-stage timings).
- **Error tracing**: every plan gets a reference id shown to the user on errors — grep logs for `"run_id": "<ref>"`.
  Unhandled exceptions are logged with full tracebacks; users only see a friendly message.
- **Graceful degradation**: each external service has a fallback (see “Failure handling” in the UI trace).
- **Guards**: per-IP rate limit (`PLAN_RATE_LIMIT`, default 6 per 10 min), concurrency cap (`MAX_CONCURRENT_PLANS`),
  daily cap (`DAILY_PLAN_LIMIT`) to protect paid quotas, input size limits, security headers.
- `GET /healthz` — liveness + which keys are missing.

## Deploy (free) — Render
1. Push this folder to a GitHub repo (`.env` and `cache/` are git-ignored).
2. On https://render.com → **New → Blueprint** → pick the repo (uses `render.yaml`, free Docker web service).
3. Enter `GEMINI_API_KEY`, `GOOGLE_MAPS_API_KEY`, `TAVILY_API_KEY` when prompted → **Apply**.
4. Public URL: `https://saturday-planner-<id>.onrender.com`. Logs: service → **Logs**.

Free-tier notes: the service sleeps after ~15 min idle (first request takes ~30–60 s to wake), and the disk cache is
reset on each deploy/restart. **Restrict your Google Maps key** (Google Cloud → Credentials → API restrictions:
Places API (New), Routes API only) and set billing alerts, since the app is public.

Other free options that run the same Dockerfile: Hugging Face Spaces (Docker SDK, set `PORT=7860`), Koyeb, Fly.io.
