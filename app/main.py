"""FastAPI app: POST /api/plan streams NDJSON (stage/trace events, then the result).

Production guards (all configurable via env): per-IP rate limit, concurrency cap, daily plan cap
(protects paid API quotas), input size limits, JSON logs with a run id on every line.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from .util import setup_logging  # noqa: E402

setup_logging()

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from .orchestrator import stream  # noqa: E402

log = logging.getLogger("planner.api")
app = FastAPI(title="Saturday Planner", docs_url=None, redoc_url=None)
STATIC = Path(__file__).resolve().parent.parent / "static"

MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_PLANS", "4"))
RATE_N, RATE_WINDOW = (int(x) for x in os.getenv("PLAN_RATE_LIMIT", "6/600").split("/"))  # 6 plans / 10 min / IP
DAILY_LIMIT = int(os.getenv("DAILY_PLAN_LIMIT", "300"))

_slots = threading.BoundedSemaphore(MAX_CONCURRENT)
_hits: dict[str, deque] = defaultdict(deque)
_daily = {"day": date.today(), "n": 0}
_guard = threading.Lock()


def _client_ip(req: Request) -> str:
    fwd = req.headers.get("x-forwarded-for")  # set by the hosting proxy
    return fwd.split(",")[0].strip() if fwd else (req.client.host if req.client else "?")


def _admit(ip: str) -> str | None:
    """Returns an error message if the request must be rejected."""
    now = time.time()
    with _guard:
        if _daily["day"] != date.today():
            _daily.update(day=date.today(), n=0)
        if _daily["n"] >= DAILY_LIMIT:
            return "The planner has reached today's usage limit. Please try again tomorrow."
        dq = _hits[ip]
        while dq and now - dq[0] > RATE_WINDOW:
            dq.popleft()
        if len(dq) >= RATE_N:
            return f"You've made {RATE_N} plans in the last {RATE_WINDOW // 60} minutes. Please wait a few minutes and try again."
        dq.append(now)
        _daily["n"] += 1
    return None


@app.middleware("http")
async def access_log(request: Request, call_next):
    t0 = time.time()
    try:
        resp = await call_next(request)
    except Exception:
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        resp = JSONResponse({"error": "Internal error"}, status_code=500)
    resp.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                         "Referrer-Policy": "strict-origin-when-cross-origin"})
    if request.url.path != "/healthz":
        log.info("%s %s -> %s (%.2fs)", request.method, request.url.path, resp.status_code, time.time() - t0,
                 extra={"event": "access", "status": resp.status_code, "duration_ms": int((time.time() - t0) * 1000),
                        "client_ip": _client_ip(request)})
    return resp


class PlanRequest(BaseModel):
    form: dict = Field(default_factory=dict)
    text: str | None = Field(None, max_length=3000)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


@app.get("/healthz")
def healthz():
    missing = [k for k in ("GEMINI_API_KEY", "GOOGLE_MAPS_API_KEY") if not os.getenv(k)]
    return {"status": "ok" if not missing else "degraded", "missing_keys": missing,
            "plans_today": _daily["n"], "daily_limit": DAILY_LIMIT}


@app.post("/api/plan")
def plan(req: PlanRequest, request: Request):
    form = {k: str(v)[:200] for k, v in (req.form or {}).items() if k in
            ("city", "area", "group_size", "available_time", "start_time", "budget", "interests", "constraints")}
    if not form and not (req.text or "").strip():
        return JSONResponse({"error": "Tell me a bit about your Saturday — where you'll start, who's coming and what you like."}, status_code=400)
    msg = _admit(_client_ip(request))
    if msg:
        return JSONResponse({"error": msg}, status_code=429)
    if not _slots.acquire(blocking=False):
        return JSONResponse({"error": "The planner is busy with other plans right now. Please try again in a minute."}, status_code=503)

    def gen():
        try:
            for ev in stream(form, (req.text or "").strip() or None):
                yield json.dumps(ev, default=str) + "\n"
        finally:
            _slots.release()
    return StreamingResponse(gen(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
