#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║          ASTRAL ALGO — MARKET MATRIX / NEWS FEED ENGINE              ║
║          Economic Calendar + AI Macro Narrative Service              ║
║          © 2026 Astral Algo. All rights reserved.                    ║
╠══════════════════════════════════════════════════════════════════════╣
║  Pulls high-impact USD economic releases from Financial Modeling     ║
║  Prep, scores directional bias (Bullish/Bearish USD), maps that      ║
║  bias onto a cross-market matrix (DXY, Majors, Inverses, Gold),      ║
║  and generates a short AI macro narrative for each release.          ║
║                                                                        ║
║  Runs as its own FastAPI app — mount it alongside main.py and        ║
║  license_engine.py, or run standalone on its own port.               ║
║                                                                        ║
║  SECURITY NOTE:                                                      ║
║  Never hardcode API keys in source. Both FMP_KEY and the AI provider ║
║  key are read from environment variables (.env). The original        ║
║  snippet this was built from had both keys hardcoded in plaintext —  ║
║  that is fixed here. Rotate any key that was ever committed in       ║
║  plaintext, since it should be treated as already compromised.       ║
║                                                                        ║
║  INSTALL:                                                            ║
║    pip install fastapi uvicorn httpx openai python-dotenv            ║
║                                                                        ║
║  RUN (standalone):                                                   ║
║    uvicorn news_feed:news_app --host 0.0.0.0 --port 8002             ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import os
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, List

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from openai import OpenAI, APIError

load_dotenv()
log = logging.getLogger("AstralAlgo.NewsFeed")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ─────────────────────────────────────────────────────────────────────
#  CONFIG — all secrets from environment, never hardcoded
# ─────────────────────────────────────────────────────────────────────

class Cfg:
    FMP_KEY        = os.getenv("FMP_API_KEY", "")
    AI_API_KEY     = os.getenv("AI_API_KEY", "")
    AI_BASE_URL    = os.getenv("AI_BASE_URL", "https://api.openai.com/v1")
    AI_MODEL       = os.getenv("AI_MODEL", "gpt-4o-mini")
    ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")
    CACHE_TTL_SEC  = int(os.getenv("NEWS_CACHE_TTL_SEC", "120"))  # avoid hammering FMP + AI on every page load
    HIGH_IMPACT_ONLY = os.getenv("NEWS_HIGH_IMPACT_ONLY", "true").lower() == "true"

cfg = Cfg()

if not cfg.FMP_KEY:
    log.warning("FMP_API_KEY not set — /api/market-feed will return an empty feed until configured.")
if not cfg.AI_API_KEY:
    log.warning("AI_API_KEY not set — narratives will fall back to a templated summary.")

ai_client: Optional[OpenAI] = OpenAI(base_url=cfg.AI_BASE_URL, api_key=cfg.AI_API_KEY) if cfg.AI_API_KEY else None

# ─────────────────────────────────────────────────────────────────────
#  DIRECTIONAL MATRIX
#  Maps a USD bias onto how correlated instruments are expected to react
# ─────────────────────────────────────────────────────────────────────

MARKET_BIAS_RULES = {
    "Bullish USD": {
        "DXY":      "▲ Strong Bullish",
        "Majors":   "▼ Bearish",     # EURUSD, GBPUSD — inverse to USD strength
        "Inverses": "▲ Bullish",     # USDJPY, USDCHF — same direction as USD
        "Gold":     "▼ Bearish",
    },
    "Bearish USD": {
        "DXY":      "▼ Strong Bearish",
        "Majors":   "▲ Bullish",
        "Inverses": "▼ Bearish",
        "Gold":     "▲ Bullish",
    },
}

# ─────────────────────────────────────────────────────────────────────
#  MODELS
# ─────────────────────────────────────────────────────────────────────

class MarketEvent(BaseModel):
    timestamp:          str
    event:               str
    currency:            str = "USD"
    actual:               float
    forecast:             float
    previous:             Optional[float] = None
    impact:               Optional[str] = None
    macro_bias:           str
    directional_matrix:   dict
    ai_narrative:         str

class MarketFeedResponse(BaseModel):
    status:        str
    generated_at:  str
    count:         int
    data:          List[MarketEvent]

# ─────────────────────────────────────────────────────────────────────
#  SIMPLE IN-MEMORY CACHE
#  FMP + AI calls cost money/quota — don't refetch on every page view
# ─────────────────────────────────────────────────────────────────────

_cache = {"data": None, "fetched_at": None}

def cache_is_fresh() -> bool:
    if _cache["data"] is None or _cache["fetched_at"] is None:
        return False
    age = (datetime.now(timezone.utc) - _cache["fetched_at"]).total_seconds()
    return age < cfg.CACHE_TTL_SEC

# ─────────────────────────────────────────────────────────────────────
#  AI NARRATIVE GENERATION
# ─────────────────────────────────────────────────────────────────────

async def generate_ai_analysis(event_name: str, actual: float, forecast: float, bias: str) -> str:
    """
    Generates a short institutional-style macro narrative for one event.
    Falls back to a templated sentence if the AI provider is unavailable,
    so the feed never breaks just because a model call failed.
    """
    if ai_client is None:
        return _fallback_narrative(event_name, actual, forecast, bias)

    prompt = (
        f"Analyze economic event: {event_name}. Actual: {actual}, Forecast: {forecast}. "
        f"Result: {bias}. Provide a sharp, 2-sentence market narrative for cross-market "
        f"exposure changes."
    )

    try:
        response = await asyncio.to_thread(
            ai_client.chat.completions.create,
            model=cfg.AI_MODEL,
            messages=[
                {"role": "system", "content": "You are a senior institutional macro strategist."},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=150,
            temperature=0.3,
        )
        return response.choices[0].message.content.strip()
    except APIError as e:
        log.error(f"AI provider error for '{event_name}': {e}")
        return _fallback_narrative(event_name, actual, forecast, bias)
    except Exception as e:
        log.error(f"Unexpected AI error for '{event_name}': {e}")
        return _fallback_narrative(event_name, actual, forecast, bias)


def _fallback_narrative(event_name: str, actual: float, forecast: float, bias: str) -> str:
    """Deterministic, no-API-call fallback so the feed degrades gracefully."""
    direction = "stronger" if bias == "Bullish USD" else "weaker"
    return (
        f"{event_name} printed {actual} vs {forecast} forecast, pointing to a {direction} USD "
        f"backdrop. Expect correlated pairs to reprice in line with the {bias.lower()} bias "
        f"until the next high-impact catalyst."
    )

# ─────────────────────────────────────────────────────────────────────
#  FMP — ECONOMIC CALENDAR FETCH
# ─────────────────────────────────────────────────────────────────────

async def fetch_fmp_calendar() -> list:
    if not cfg.FMP_KEY:
        return []

    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url = (
        "https://financialmodelingprep.com/api/v3/economic_calendar"
        f"?from={current_date}&to={current_date}&apikey={cfg.FMP_KEY}"
    )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
            if r.status_code != 200:
                log.error(f"FMP error {r.status_code}: {r.text[:200]}")
                raise HTTPException(status_code=502, detail="FMP data pipeline offline")
            return r.json()
    except httpx.RequestError as e:
        log.error(f"FMP request failed: {e}")
        raise HTTPException(status_code=502, detail="FMP data pipeline unreachable")

# ─────────────────────────────────────────────────────────────────────
#  CORE PROCESSING
# ─────────────────────────────────────────────────────────────────────

async def build_market_feed() -> List[dict]:
    raw_events = await fetch_fmp_calendar()
    processed  = []

    # Process events concurrently — each one needs an AI call,
    # sequential awaiting would make the page load painfully slow
    tasks = []
    meta  = []

    for item in raw_events:
        actual   = item.get("actual")
        forecast = item.get("forecast")
        currency = item.get("currency")
        impact   = (item.get("impact") or "").lower()

        if currency != "USD" or actual is None or forecast is None:
            continue

        if cfg.HIGH_IMPACT_ONLY and impact and impact not in ("high", "medium"):
            continue

        try:
            actual_f, forecast_f = float(actual), float(forecast)
        except (TypeError, ValueError):
            continue

        if actual_f > forecast_f:
            bias = "Bullish USD"
        elif actual_f < forecast_f:
            bias = "Bearish USD"
        else:
            bias = "Neutral"

        if bias == "Neutral":
            continue  # No directional edge — skip from the feed

        event_name = item.get("event", "Unknown Event")
        tasks.append(generate_ai_analysis(event_name, actual_f, forecast_f, bias))
        meta.append({
            "event":     event_name,
            "actual":    actual_f,
            "forecast":  forecast_f,
            "previous":  item.get("previous"),
            "impact":    item.get("impact"),
            "bias":      bias,
        })

    narratives = await asyncio.gather(*tasks) if tasks else []

    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
    for m, narrative in zip(meta, narratives):
        processed.append({
            "timestamp":           now_str,
            "event":               m["event"],
            "currency":            "USD",
            "actual":              m["actual"],
            "forecast":            m["forecast"],
            "previous":            m["previous"],
            "impact":              m["impact"],
            "macro_bias":          m["bias"],
            "directional_matrix":  MARKET_BIAS_RULES[m["bias"]],
            "ai_narrative":        narrative,
        })

    return processed

# ─────────────────────────────────────────────────────────────────────
#  FASTAPI APP
# ─────────────────────────────────────────────────────────────────────

news_app = FastAPI(
    title="Astral Algo — Market Matrix API",
    description="Economic calendar + AI macro narrative feed",
    version="1.0.0",
)

news_app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@news_app.get("/api/market-feed", response_model=MarketFeedResponse, tags=["Market Matrix"])
async def get_market_analysis(force_refresh: bool = Query(False, description="Bypass cache")):
    """
    Returns today's high-impact USD economic releases with directional
    bias, a cross-market matrix, and an AI-generated macro narrative.
    Cached for CACHE_TTL_SEC seconds to control FMP/AI usage.
    """
    if not force_refresh and cache_is_fresh():
        return {
            "status":       "success",
            "generated_at": _cache["fetched_at"].isoformat(),
            "count":        len(_cache["data"]),
            "data":         _cache["data"],
        }

    try:
        feed = await build_market_feed()
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Market feed build failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    _cache["data"]       = feed
    _cache["fetched_at"] = datetime.now(timezone.utc)

    return {
        "status":       "success",
        "generated_at": _cache["fetched_at"].isoformat(),
        "count":        len(feed),
        "data":         feed,
    }


@news_app.get("/api/market-feed/health", tags=["System"])
async def health():
    return {
        "status":          "ok",
        "fmp_configured":  bool(cfg.FMP_KEY),
        "ai_configured":   bool(cfg.AI_API_KEY),
        "cache_age_sec":   (
            (datetime.now(timezone.utc) - _cache["fetched_at"]).total_seconds()
            if _cache["fetched_at"] else None
        ),
        "server_time":     datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("news_feed:news_app", host="0.0.0.0", port=8002, reload=True, log_level="info")
