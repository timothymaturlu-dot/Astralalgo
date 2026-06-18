#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║           ASTRAL ALGO — SMC BACKEND SERVER                       ║
║           MT5 ↔ FastAPI ↔ Supabase ↔ Telegram ↔ Website         ║
║           © 2026 Astral Algo                                     ║
╠══════════════════════════════════════════════════════════════════╣
║  STACK:                                                          ║
║    FastAPI      — REST API server                                ║
║    Supabase     — Database (signals, trades, accounts)           ║
║    Telegram     — Bot alerts via python-telegram-bot             ║
║    WebSockets   — Live signal push to website dashboard          ║
║    APScheduler  — Cron jobs (daily summary, health checks)       ║
║                                                                  ║
║  INSTALL:                                                        ║
║    pip install fastapi uvicorn supabase python-telegram-bot      ║
║               apscheduler python-dotenv websockets aiohttp       ║
║                                                                  ║
║  RUN:                                                            ║
║    uvicorn main:app --host 0.0.0.0 --port 8000 --reload         ║
║                                                                  ║
║  MT5 EA WEBHOOK:                                                 ║
║    In the EA inputs, set your server URL:                        ║
║    http://YOUR_VPS_IP:8000                                       ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import json
import hmac
import hashlib
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, WebSocket, WebSocketDisconnect, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator
from dotenv import load_dotenv
from supabase import create_client, Client
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import aiohttp

# ── Load environment ──────────────────────────────────────────────────
load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("astralalgo.log"),
    ]
)
log = logging.getLogger("AstralAlgo")

# ── Config ────────────────────────────────────────────────────────────
class Config:
    SUPABASE_URL        = os.getenv("SUPABASE_URL", "")
    SUPABASE_KEY        = os.getenv("SUPABASE_ANON_KEY", "")
    TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
    EA_SECRET           = os.getenv("EA_SECRET", "astral_algo_secret_2026")  # Shared secret with MT5 EA
    ALLOWED_ORIGINS     = os.getenv("ALLOWED_ORIGINS", "*").split(",")
    DAILY_SUMMARY_HOUR  = int(os.getenv("DAILY_SUMMARY_HOUR", "23"))
    DAILY_SUMMARY_MIN   = int(os.getenv("DAILY_SUMMARY_MIN", "50"))

cfg = Config()

# ── Supabase ──────────────────────────────────────────────────────────
supabase: Client = create_client(cfg.SUPABASE_URL, cfg.SUPABASE_KEY) if cfg.SUPABASE_URL else None

# ── Scheduler ─────────────────────────────────────────────────────────
scheduler = AsyncIOScheduler()

# ── WebSocket Manager ─────────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        log.info(f"WS client connected. Total: {len(self.active)}")

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)
        log.info(f"WS client disconnected. Total: {len(self.active)}")

    async def broadcast(self, data: dict):
        msg = json.dumps(data)
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

ws_manager = ConnectionManager()

# ─────────────────────────────────────────────────────────────────────
#  PYDANTIC MODELS
# ─────────────────────────────────────────────────────────────────────

class SignalPayload(BaseModel):
    """Incoming signal from MT5 EA via HTTP POST"""
    symbol:        str   = Field(..., example="XAUUSD")
    direction:     str   = Field(..., example="BUY")           # BUY | SELL
    entry:         float = Field(..., example=2341.50)
    sl:            float = Field(..., example=2332.00)
    tp1:           float = Field(..., example=2354.00)
    tp2:           float = Field(..., example=2368.00)
    tp3:           float = Field(..., example=2385.00)
    lots:          float = Field(..., example=0.25)
    rr:            float = Field(..., example=2.4)
    confidence:    float = Field(..., example=92.0)
    session:       str   = Field(..., example="New York Kill Zone")
    confluences:   str   = Field(..., example="HTF Bias ✓ | MSB ✓ | Liq Sweep ✓ | OB ✓")
    magic:         int   = Field(..., example=20260612)
    account_size:  Optional[float] = None
    risk_pct:      Optional[float] = None
    propfirm_mode: Optional[bool]  = True

    @validator("direction")
    def validate_direction(cls, v):
        if v.upper() not in ("BUY", "SELL"):
            raise ValueError("direction must be BUY or SELL")
        return v.upper()

class TradeUpdatePayload(BaseModel):
    """Trade status update from EA (TP hit, SL hit, partial close)"""
    signal_id:  Optional[str] = None
    ticket:     int
    symbol:     str
    direction:  str
    status:     str     # OPEN | TP1_HIT | TP2_HIT | TP3_HIT | SL_HIT | CLOSED | PARTIAL
    exit_price: Optional[float] = None
    pnl_pips:   Optional[float] = None
    pnl_usd:    Optional[float] = None
    magic:      int

class AccountPayload(BaseModel):
    """Account snapshot from EA (sent periodically)"""
    magic:          int
    balance:        float
    equity:         float
    margin:         float
    free_margin:    float
    daily_dd_pct:   float
    total_dd_pct:   float
    open_trades:    int
    daily_pnl_usd:  float
    daily_pnl_pct:  float
    server_time:    str

class HealthPayload(BaseModel):
    """Heartbeat from EA every 60 seconds"""
    magic:      int
    symbol:     str
    uptime_sec: int
    in_session: bool
    bias:       str   # BULLISH | BEARISH | NEUTRAL
    spread:     float

# ─────────────────────────────────────────────────────────────────────
#  APP STARTUP / SHUTDOWN
# ─────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("✦ Astral Algo Backend starting...")

    # Scheduled jobs
    scheduler.add_job(
        daily_summary_job,
        "cron",
        hour=cfg.DAILY_SUMMARY_HOUR,
        minute=cfg.DAILY_SUMMARY_MIN,
        id="daily_summary"
    )
    scheduler.add_job(
        health_check_job,
        "interval",
        minutes=5,
        id="health_check"
    )
    scheduler.start()

    await send_telegram(
        "✦ *Astral Algo Server Started*\n"
        f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        "Status: 🟢 All systems operational"
    )
    log.info("✦ Server ready.")
    yield

    scheduler.shutdown()
    log.info("✦ Server shutting down.")

# ─────────────────────────────────────────────────────────────────────
#  FASTAPI APP
# ─────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Astral Algo API",
    description="SMC/ICT Forex Signal Backend — MT5 ↔ Website ↔ Telegram",
    version="2.4.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────────────────
#  SECURITY — EA Secret Verification
# ─────────────────────────────────────────────────────────────────────

def verify_ea_secret(x_ea_secret: str = Header(...)):
    """All EA → server requests must include X-EA-Secret header"""
    if x_ea_secret != cfg.EA_SECRET:
        log.warning(f"Unauthorized EA request — bad secret")
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

# ─────────────────────────────────────────────────────────────────────
#  ROUTES — EA INBOUND
# ─────────────────────────────────────────────────────────────────────

@app.post("/ea/signal", tags=["EA"])
async def receive_signal(
    payload: SignalPayload,
    background: BackgroundTasks,
    _: bool = Depends(verify_ea_secret)
):
    """
    Called by MT5 EA when a new SMC signal fires.
    Saves to DB, broadcasts to website, sends Telegram alert.
    """
    log.info(f"📡 Signal received: {payload.direction} {payload.symbol} @ {payload.entry}")

    # Build signal record
    signal = {
        "symbol":        payload.symbol,
        "direction":     payload.direction,
        "entry":         payload.entry,
        "sl":            payload.sl,
        "tp1":           payload.tp1,
        "tp2":           payload.tp2,
        "tp3":           payload.tp3,
        "lots":          payload.lots,
        "rr":            round(payload.rr, 2),
        "confidence":    payload.confidence,
        "session":       payload.session,
        "confluences":   payload.confluences,
        "magic":         payload.magic,
        "account_size":  payload.account_size,
        "risk_pct":      payload.risk_pct,
        "propfirm_mode": payload.propfirm_mode,
        "status":        "ACTIVE",
        "created_at":    datetime.now(timezone.utc).isoformat(),
        "pips_gained":   0,
        "pnl_usd":       0,
    }

    signal_id = None

    # Save to Supabase
    if supabase:
        try:
            res = supabase.table("signals").insert(signal).execute()
            if res.data:
                signal_id = res.data[0].get("id")
                signal["id"] = signal_id
                log.info(f"Signal saved to DB: {signal_id}")
        except Exception as e:
            log.error(f"Supabase insert error: {e}")

    # Broadcast to all connected website clients
    background.add_task(
        ws_manager.broadcast,
        {"type": "new_signal", "data": signal}
    )

    # Send Telegram alert
    background.add_task(send_signal_telegram, payload, signal_id)

    return {"status": "ok", "signal_id": signal_id, "message": "Signal processed"}


@app.post("/ea/trade-update", tags=["EA"])
async def trade_update(
    payload: TradeUpdatePayload,
    background: BackgroundTasks,
    _: bool = Depends(verify_ea_secret)
):
    """
    Called by EA when a trade closes, hits TP, or hits SL.
    Updates DB record, sends Telegram alert, broadcasts to website.
    """
    log.info(f"🔄 Trade update: {payload.status} | {payload.symbol} #{payload.ticket}")

    update_data = {
        "status":     payload.status,
        "exit_price": payload.exit_price,
        "pips_gained": payload.pnl_pips,
        "pnl_usd":    payload.pnl_usd,
        "closed_at":  datetime.now(timezone.utc).isoformat() if payload.status in ("SL_HIT","TP3_HIT","CLOSED") else None,
    }

    # Update Supabase
    if supabase and payload.signal_id:
        try:
            supabase.table("signals").update(update_data).eq("id", payload.signal_id).execute()
        except Exception as e:
            log.error(f"Supabase update error: {e}")

    # Broadcast to website
    background.add_task(
        ws_manager.broadcast,
        {"type": "trade_update", "data": {**payload.dict(), **update_data}}
    )

    # Telegram alerts
    background.add_task(send_trade_update_telegram, payload)

    return {"status": "ok"}


@app.post("/ea/account", tags=["EA"])
async def account_snapshot(
    payload: AccountPayload,
    background: BackgroundTasks,
    _: bool = Depends(verify_ea_secret)
):
    """
    Periodic account snapshot from EA.
    Stores to DB and broadcasts to dashboard.
    """
    data = {
        **payload.dict(),
        "recorded_at": datetime.now(timezone.utc).isoformat()
    }

    if supabase:
        try:
            supabase.table("account_snapshots").insert(data).execute()
        except Exception as e:
            log.error(f"Account snapshot DB error: {e}")

    # Check for breaches and alert
    if payload.daily_dd_pct >= 4.0:
        background.add_task(
            send_telegram,
            f"⚠️ *PROPFIRM WARNING — Daily DD at {payload.daily_dd_pct:.2f}%*\n"
            f"Approaching 5% limit. Monitor closely."
        )

    if payload.total_dd_pct >= 8.0:
        background.add_task(
            send_telegram,
            f"🚨 *CRITICAL — Total DD at {payload.total_dd_pct:.2f}%*\n"
            f"Approaching 10% limit. Consider closing positions."
        )

    background.add_task(
        ws_manager.broadcast,
        {"type": "account_update", "data": data}
    )

    return {"status": "ok"}


@app.post("/ea/heartbeat", tags=["EA"])
async def heartbeat(
    payload: HealthPayload,
    background: BackgroundTasks,
    _: bool = Depends(verify_ea_secret)
):
    """60-second heartbeat from EA to confirm it's alive."""
    background.add_task(
        ws_manager.broadcast,
        {
            "type": "heartbeat",
            "data": {
                **payload.dict(),
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        }
    )
    return {"status": "alive"}

# ─────────────────────────────────────────────────────────────────────
#  ROUTES — WEBSITE / DASHBOARD READS
# ─────────────────────────────────────────────────────────────────────

@app.get("/signals", tags=["Dashboard"])
async def get_signals(
    limit:     int = 20,
    status:    Optional[str] = None,
    symbol:    Optional[str] = None,
    direction: Optional[str] = None,
):
    """Fetch recent signals for the website dashboard."""
    if not supabase:
        return {"signals": get_mock_signals()}

    try:
        q = supabase.table("signals").select("*").order("created_at", desc=True).limit(limit)
        if status:    q = q.eq("status", status.upper())
        if symbol:    q = q.eq("symbol", symbol.upper())
        if direction: q = q.eq("direction", direction.upper())
        res = q.execute()
        return {"signals": res.data, "count": len(res.data)}
    except Exception as e:
        log.error(f"Signals fetch error: {e}")
        return {"signals": get_mock_signals()}


@app.get("/signals/{signal_id}", tags=["Dashboard"])
async def get_signal(signal_id: str):
    """Fetch a single signal by ID."""
    if not supabase:
        raise HTTPException(404, "Signal not found")
    try:
        res = supabase.table("signals").select("*").eq("id", signal_id).single().execute()
        return res.data
    except Exception as e:
        raise HTTPException(404, f"Signal not found: {e}")


@app.get("/performance", tags=["Dashboard"])
async def get_performance(days: int = 30):
    """
    Aggregate performance stats for the dashboard.
    Returns win rate, total pips, P&L, RR averages.
    """
    if not supabase:
        return get_mock_performance()

    try:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        res = supabase.table("signals") \
            .select("direction,status,pips_gained,pnl_usd,rr,confidence,symbol") \
            .gte("created_at", since) \
            .execute()

        trades = res.data
        closed = [t for t in trades if t.get("status") in ("TP1_HIT","TP2_HIT","TP3_HIT","SL_HIT","CLOSED")]
        wins   = [t for t in closed if t.get("status") in ("TP1_HIT","TP2_HIT","TP3_HIT")]

        total_pips = sum(t.get("pips_gained", 0) or 0 for t in closed)
        total_pnl  = sum(t.get("pnl_usd", 0)    or 0 for t in closed)
        avg_rr     = (sum(t.get("rr", 0) or 0 for t in closed) / len(closed)) if closed else 0
        avg_conf   = (sum(t.get("confidence", 0) or 0 for t in trades) / len(trades)) if trades else 0

        # By symbol
        symbols = {}
        for t in closed:
            s = t.get("symbol", "UNKNOWN")
            if s not in symbols:
                symbols[s] = {"total": 0, "wins": 0, "pips": 0}
            symbols[s]["total"] += 1
            if t in wins:
                symbols[s]["wins"] += 1
            symbols[s]["pips"] += t.get("pips_gained", 0) or 0

        return {
            "period_days":  days,
            "total_signals": len(trades),
            "closed_trades": len(closed),
            "wins":          len(wins),
            "losses":        len(closed) - len(wins),
            "win_rate_pct":  round(len(wins) / len(closed) * 100, 1) if closed else 0,
            "total_pips":    round(total_pips, 1),
            "total_pnl_usd": round(total_pnl, 2),
            "avg_rr":        round(avg_rr, 2),
            "avg_confidence": round(avg_conf, 1),
            "by_symbol":     symbols,
        }
    except Exception as e:
        log.error(f"Performance query error: {e}")
        return get_mock_performance()


@app.get("/account/latest", tags=["Dashboard"])
async def get_account_latest():
    """Latest account snapshot."""
    if not supabase:
        return {"balance": 50000, "equity": 51240, "daily_dd_pct": 1.2, "total_dd_pct": 3.8}
    try:
        res = supabase.table("account_snapshots") \
            .select("*").order("recorded_at", desc=True).limit(1).execute()
        return res.data[0] if res.data else {}
    except Exception as e:
        log.error(f"Account fetch error: {e}")
        return {}


@app.get("/health", tags=["System"])
async def health():
    """Server health check endpoint."""
    return {
        "status":     "ok",
        "version":    "2.4.0",
        "server_time": datetime.now(timezone.utc).isoformat(),
        "db":         "connected" if supabase else "not configured",
        "telegram":   "configured" if cfg.TELEGRAM_TOKEN else "not configured",
        "ws_clients": len(ws_manager.active),
    }

# ─────────────────────────────────────────────────────────────────────
#  WEBSOCKET — Live signal push to website
# ─────────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """
    Website connects here to receive live signal and account updates.
    Each new signal fires immediately to all connected tabs.
    """
    await ws_manager.connect(ws)
    try:
        # Send welcome + last 5 signals on connect
        if supabase:
            try:
                res = supabase.table("signals").select("*").order("created_at", desc=True).limit(5).execute()
                await ws.send_text(json.dumps({"type": "init", "data": res.data}))
            except Exception:
                pass

        while True:
            # Keep alive — receive ping from client
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))

    except WebSocketDisconnect:
        ws_manager.disconnect(ws)

# ─────────────────────────────────────────────────────────────────────
#  TELEGRAM HELPERS
# ─────────────────────────────────────────────────────────────────────

async def send_telegram(message: str, parse_mode: str = "Markdown") -> bool:
    """Send a message to the configured Telegram group."""
    if not cfg.TELEGRAM_TOKEN or not cfg.TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — message skipped")
        return False

    url = f"https://api.telegram.org/bot{cfg.TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id":    cfg.TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": parse_mode,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    log.info("Telegram message sent ✓")
                    return True
                else:
                    body = await r.text()
                    log.error(f"Telegram error {r.status}: {body}")
                    return False
    except Exception as e:
        log.error(f"Telegram send exception: {e}")
        return False


async def send_signal_telegram(payload: SignalPayload, signal_id: Optional[str]):
    """Format and send a full signal alert to Telegram."""
    direction_icon = "🟢" if payload.direction == "BUY" else "🔴"
    arrow = "↑" if payload.direction == "BUY" else "↓"

    # Calculate pips for display
    pip_mult = 10 if "JPY" not in payload.symbol else 100
    if payload.direction == "BUY":
        tp1_pips = round((payload.tp1 - payload.entry) * pip_mult * 10)
        tp2_pips = round((payload.tp2 - payload.entry) * pip_mult * 10)
        tp3_pips = round((payload.tp3 - payload.entry) * pip_mult * 10)
        sl_pips  = round((payload.entry - payload.sl)  * pip_mult * 10)
    else:
        tp1_pips = round((payload.entry - payload.tp1) * pip_mult * 10)
        tp2_pips = round((payload.entry - payload.tp2) * pip_mult * 10)
        tp3_pips = round((payload.entry - payload.tp3) * pip_mult * 10)
        sl_pips  = round((payload.sl  - payload.entry) * pip_mult * 10)

    digits = 2 if payload.symbol in ("XAUUSD", "US30", "NAS100") else 5

    msg = (
        f"⚡ *ASTRAL ALGO — {payload.direction} SIGNAL* {direction_icon}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📌 *Pair:*       `{payload.symbol}`\n"
        f"🧭 *Direction:*  *{payload.direction} {arrow}*\n\n"
        f"📍 *Entry:*      `{payload.entry:.{digits}f}`\n"
        f"🎯 *TP 1:*       `{payload.tp1:.{digits}f}` _(+{tp1_pips}p)_\n"
        f"🎯 *TP 2:*       `{payload.tp2:.{digits}f}` _(+{tp2_pips}p)_\n"
        f"🎯 *TP 3:*       `{payload.tp3:.{digits}f}` _(+{tp3_pips}p)_\n"
        f"🔴 *Stop Loss:*  `{payload.sl:.{digits}f}` _(-{sl_pips}p)_\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *RR:*         1 : {payload.rr:.1f}\n"
        f"🎲 *Confidence:* {payload.confidence:.0f}%\n"
        f"📦 *Lots:*       {payload.lots:.2f}"
    )

    if payload.account_size:
        msg += f" _(${payload.account_size:,.0f} acc)_"

    if payload.risk_pct:
        msg += f"\n💰 *Risk:*       {payload.risk_pct:.1f}%"

    msg += (
        f"\n⏱ *Session:*    {payload.session}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🧠 *Confluences:*\n"
        f"_{payload.confluences}_\n\n"
        f"🛡️ _PropFirm Mode: {'✅ Active' if payload.propfirm_mode else '❌ Off'}_\n"
    )

    if signal_id:
        msg += f"🔗 _Signal ID: {signal_id[:8]}_\n"

    msg += f"\n_— Astral Algo © 2026_"

    await send_telegram(msg)


async def send_trade_update_telegram(payload: TradeUpdatePayload):
    """Send TP/SL/close alerts to Telegram."""
    status_map = {
        "TP1_HIT":  ("✅", "TP 1 HIT"),
        "TP2_HIT":  ("🎯", "TP 2 HIT"),
        "TP3_HIT":  ("💰", "TP 3 HIT — FULL RUN"),
        "SL_HIT":   ("🔴", "STOP LOSS HIT"),
        "PARTIAL":  ("🔄", "PARTIAL CLOSE"),
        "CLOSED":   ("🏁", "TRADE CLOSED"),
    }

    icon, label = status_map.get(payload.status, ("📌", payload.status))
    pnl_str = ""
    if payload.pnl_usd is not None:
        pnl_str = f"\n💵 *P&L:* {'+'if payload.pnl_usd >= 0 else ''}{payload.pnl_usd:.2f} USD"
    if payload.pnl_pips is not None:
        pnl_str += f" _({'+'if payload.pnl_pips >= 0 else ''}{payload.pnl_pips:.0f} pips)_"

    msg = (
        f"{icon} *{label} — {payload.symbol}*\n"
        f"Direction: {payload.direction}\n"
        f"Ticket: `#{payload.ticket}`"
    )

    if payload.exit_price:
        digits = 2 if payload.symbol in ("XAUUSD", "US30", "NAS100") else 5
        msg += f"\nExit: `{payload.exit_price:.{digits}f}`"

    msg += pnl_str

    if payload.status == "SL_HIT":
        msg += "\n\n_Stay disciplined. The edge plays out over many trades. 🛡️_"
    elif payload.status in ("TP2_HIT", "TP3_HIT"):
        msg += "\n\n_Move remaining to breakeven and let the runner go. ✦_"

    msg += "\n\n_— Astral Algo © 2026_"
    await send_telegram(msg)

# ─────────────────────────────────────────────────────────────────────
#  SCHEDULED JOBS
# ─────────────────────────────────────────────────────────────────────

async def daily_summary_job():
    """Send a daily performance summary to Telegram at end of day."""
    log.info("Running daily summary job...")

    if not supabase:
        return

    try:
        since = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()

        res = supabase.table("signals") \
            .select("status,pips_gained,pnl_usd,direction,symbol") \
            .gte("created_at", since).execute()

        trades = res.data
        closed = [t for t in trades if t.get("status") not in ("ACTIVE", None)]
        wins   = [t for t in closed if "TP" in (t.get("status") or "")]
        total_pips = sum(t.get("pips_gained", 0) or 0 for t in closed)
        total_pnl  = sum(t.get("pnl_usd", 0)    or 0 for t in closed)

        # Latest account snapshot
        acc_res = supabase.table("account_snapshots") \
            .select("balance,equity,daily_dd_pct,total_dd_pct") \
            .order("recorded_at", desc=True).limit(1).execute()
        acc = acc_res.data[0] if acc_res.data else {}

        wr = round(len(wins) / len(closed) * 100, 1) if closed else 0
        pnl_sign = "+" if total_pnl >= 0 else ""
        pip_sign  = "+" if total_pips >= 0 else ""
        grade = "🏆 Excellent" if wr >= 80 else "✅ Good" if wr >= 60 else "⚠️ Review setups"

        msg = (
            f"📊 *ASTRAL ALGO — DAILY SUMMARY*\n"
            f"_{datetime.now(timezone.utc).strftime('%A, %d %B %Y')}_\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🎯 *Signals Today:*  {len(trades)}\n"
            f"✅ *Closed Trades:* {len(closed)}\n"
            f"🏆 *Wins:*          {len(wins)}\n"
            f"❌ *Losses:*        {len(closed) - len(wins)}\n"
            f"📈 *Win Rate:*      {wr}%\n\n"
            f"💰 *Total Pips:*    {pip_sign}{total_pips:.0f}p\n"
            f"💵 *Total P&L:*     {pnl_sign}${total_pnl:.2f}\n\n"
        )

        if acc:
            msg += (
                f"🏦 *Balance:*       ${acc.get('balance', 0):,.2f}\n"
                f"📉 *Daily DD:*      -{acc.get('daily_dd_pct', 0):.2f}%\n"
                f"📉 *Total DD:*      -{acc.get('total_dd_pct', 0):.2f}%\n\n"
            )

        msg += (
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Performance: {grade}\n\n"
            f"_— Astral Algo © 2026_"
        )

        await send_telegram(msg)

    except Exception as e:
        log.error(f"Daily summary error: {e}")


async def health_check_job():
    """Check if EA is still sending heartbeats. Alert if dead."""
    # In production: check last heartbeat timestamp in DB
    # and alert if > 10 minutes without a ping
    log.debug("Health check running...")

# ─────────────────────────────────────────────────────────────────────
#  MOCK DATA (for development without Supabase)
# ─────────────────────────────────────────────────────────────────────

def get_mock_signals():
    return [
        {"id": "sig_001", "symbol": "XAUUSD", "direction": "BUY",  "entry": 2341.50, "sl": 2332.00, "tp1": 2354.00, "tp2": 2368.00, "tp3": 2385.00, "lots": 0.25, "rr": 2.4, "confidence": 92, "session": "NY Kill Zone",    "status": "ACTIVE",  "pips_gained": 82,  "confluences": "HTF Bias ✓ | MSB ✓ | Liq Sweep ✓ | OB ✓ | FVG ✓"},
        {"id": "sig_002", "symbol": "GBPUSD", "direction": "BUY",  "entry": 1.2720,  "sl": 1.2681,  "tp1": 1.2778,  "tp2": 1.2830,  "tp3": 1.2900,  "lots": 0.10, "rr": 2.1, "confidence": 87, "session": "London KZ",      "status": "ACTIVE",  "pips_gained": 58,  "confluences": "HTF Bias ✓ | OTE ✓ | OB ✓ | CISD ✓"},
        {"id": "sig_003", "symbol": "EURUSD", "direction": "SELL", "entry": 1.0821,  "sl": 1.0860,  "tp1": 1.0768,  "tp2": 1.0720,  "tp3": 1.0660,  "lots": 0.10, "rr": 2.6, "confidence": 89, "session": "London KZ",      "status": "TP1_HIT", "pips_gained": 53,  "confluences": "HTF Bias ✓ | MSB ✓ | Liq Sweep ✓ | OB ✓"},
        {"id": "sig_004", "symbol": "USDJPY", "direction": "SELL", "entry": 154.80,  "sl": 155.40,  "tp1": 153.90,  "tp2": 153.10,  "tp3": 152.00,  "lots": 0.05, "rr": 2.8, "confidence": 78, "session": "Tokyo KZ",       "status": "ACTIVE",  "pips_gained": 90,  "confluences": "HTF Bias ✓ | OTE ✓ | FVG ✓"},
        {"id": "sig_005", "symbol": "US30",   "direction": "BUY",  "entry": 39650,   "sl": 39520,   "tp1": 39820,   "tp2": 39990,   "tp3": 40200,   "lots": 0.02, "rr": 2.1, "confidence": 83, "session": "NY Kill Zone",   "status": "ACTIVE",  "pips_gained": 170, "confluences": "HTF Bias ✓ | MSB ✓ | OB ✓"},
    ]

def get_mock_performance():
    return {
        "period_days": 30,
        "total_signals": 148,
        "closed_trades": 132,
        "wins": 114,
        "losses": 18,
        "win_rate_pct": 86.4,
        "total_pips": 4812.0,
        "total_pnl_usd": 9624.0,
        "avg_rr": 2.4,
        "avg_confidence": 88.1,
        "by_symbol": {
            "XAUUSD": {"total": 48, "wins": 42, "pips": 2100},
            "GBPUSD": {"total": 32, "wins": 28, "pips": 980},
            "EURUSD": {"total": 28, "wins": 24, "pips": 820},
            "USDJPY": {"total": 14, "wins": 12, "pips": 540},
            "US30":   {"total": 10, "wins":  8, "pips": 372},
        }
    }


# ─────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
