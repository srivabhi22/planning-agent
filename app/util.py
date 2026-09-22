"""Cache, geo math, LLM helper."""
from __future__ import annotations

import hashlib
import json
import math
import os
import logging
import threading
import time
import uuid
import contextvars
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Type, TypeVar

import httpx
from pydantic import BaseModel

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)
_lock = threading.Lock()

log = logging.getLogger("planner")


# ---------------- per-request state ----------------
# Everything that used to be a module global lives here, so concurrent users never share state.
@dataclass
class RunState:
    run_id: str = "-"
    target_date: Optional[str] = None      # planned Saturday, YYYY-MM-DD
    transport: Any = None                  # travel.TransportProfile
    holiday: Any = None                    # holidays.HolidayInfo
    radius_factor: float = 1.0             # >1 during the relaxed retry
    llm_calls: int = 0
    llm_failures: int = 0
    http_calls: int = 0
    stage_times: dict = field(default_factory=dict)


_RUN: contextvars.ContextVar[RunState] = contextvars.ContextVar("run_state", default=RunState())


def run_state() -> RunState:
    return _RUN.get()


def new_run() -> RunState:
    st = RunState(run_id=uuid.uuid4().hex[:10])
    _RUN.set(st)
    return st


def submit(ex, fn, *args, **kwargs):
    """ThreadPoolExecutor.submit that carries the request's context (run state + log run_id) into the worker."""
    return ex.submit(contextvars.copy_context().run, fn, *args, **kwargs)


class RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = run_state().run_id
        return True


class JsonFormatter(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        out = {"ts": self.formatTime(r, "%Y-%m-%dT%H:%M:%S"), "level": r.levelname, "logger": r.name,
               "run_id": getattr(r, "run_id", "-"), "msg": r.getMessage()}
        for k in ("event", "duration_ms", "status", "stage", "client_ip"):
            if hasattr(r, k):
                out[k] = getattr(r, k)
        if r.exc_info:
            out["exc"] = self.formatException(r.exc_info)
        return json.dumps(out, ensure_ascii=False)


def setup_logging() -> None:
    """LOG_FORMAT=json for production log aggregation, 'text' (default) for local dev."""
    h = logging.StreamHandler()
    h.addFilter(RunIdFilter())
    if os.getenv("LOG_FORMAT", "text") == "json":
        h.setFormatter(JsonFormatter())
    else:
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(run_id)s] %(name)s: %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(os.getenv("LOG_LEVEL", "INFO"))
    for noisy in ("httpx", "httpcore", "google_genai", "urllib3", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


UA = {"User-Agent": "perfect-saturday-planner/1.0 (demo)"}


def _req_start(req: httpx.Request):
    req.extensions["t0"] = time.time()


def _req_end(resp: httpx.Response):
    t0 = resp.request.extensions.get("t0", time.time())
    lvl = logging.INFO if resp.status_code < 400 else logging.WARNING
    dt = time.time() - t0
    run_state().http_calls += 1
    # host + path only: never log query strings (they can carry API keys)
    log.log(lvl, "HTTP %s %s%s -> %s (%.2fs)", resp.request.method, resp.request.url.host, resp.request.url.path,
            resp.status_code, dt, extra={"event": "http", "status": resp.status_code, "duration_ms": int(dt * 1000)})


HTTP = httpx.Client(timeout=httpx.Timeout(25, connect=8), headers=UA,
                    limits=httpx.Limits(max_connections=50, max_keepalive_connections=20), event_hooks={"request": [_req_start], "response": [_req_end]})


# ---------------- cache ----------------
def _cpath(ns: str, key: str) -> Path:
    h = hashlib.sha1(key.encode()).hexdigest()[:20]
    d = CACHE_DIR / ns
    d.mkdir(exist_ok=True)
    return d / f"{h}.json"


def cache_get(ns: str, key: str) -> Optional[Any]:
    p = _cpath(ns, key)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return None
    return None


def cache_set(ns: str, key: str, val: Any) -> None:
    with _lock:
        _cpath(ns, key).write_text(json.dumps(val, default=str))


# ---------------- geo ----------------
def haversine_km(a_lat, a_lon, b_lat, b_lon) -> float:
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(x))


def maps_link(name: str, lat: float, lon: float) -> str:
    from urllib.parse import quote
    return f"https://www.google.com/maps/search/?api=1&query={quote(name)}%20{lat:.6f},{lon:.6f}"


# ---------------- LLM (Gemini) ----------------
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
_client = None

T = TypeVar("T", bound=BaseModel)


def client():
    global _client
    if _client is None:
        from google import genai
        from google.genai import types
        _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"), http_options=types.HttpOptions(timeout=60_000))
    return _client


def _transient(e: Exception) -> bool:
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    return code in (500, 502, 503, 504) or isinstance(e, (TimeoutError, httpx.TimeoutException, httpx.NetworkError))


def llm_parse(system: str, user: str, schema: Type[T], effort: str = "low", max_tokens: int = 16000) -> T:
    """Structured-output call. One retry on transient errors (not on quota 429). Raises on failure; callers own the fallback."""
    from google.genai import types
    st = run_state()
    name = schema.__name__.strip("_")
    for attempt in (1, 2):
        t0 = time.time()
        st.llm_calls += 1
        try:
            resp = _gen(types, system, user, schema, max_tokens)
            dt = time.time() - t0
            log.info("LLM %s ok (%.2fs)", name, dt, extra={"event": "llm", "status": "ok", "duration_ms": int(dt * 1000)})
            if isinstance(resp.parsed, schema):
                return resp.parsed
            if resp.text:
                return schema.model_validate_json(resp.text)
            raise RuntimeError("LLM returned no structured output")
        except Exception as e:
            dt = time.time() - t0
            if attempt == 1 and _transient(e):
                log.warning("LLM %s transient error, retrying: %s", name, str(e)[:200], extra={"event": "llm", "status": "retry"})
                time.sleep(1.5)
                continue
            st.llm_failures += 1
            log.error("LLM %s failed after %.2fs: %s", name, dt, str(e)[:300],
                      extra={"event": "llm", "status": "error", "duration_ms": int(dt * 1000)})
            raise


def _gen(types, system, user, schema, max_tokens):
    return client().models.generate_content(
        model=MODEL,
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            max_output_tokens=max_tokens,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )
