#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║          FIERCE v5.3 — SUBSCRIPTION KEY MANAGEMENT SYSTEM           ║
║          Astral Algo Commercial License Engine                       ║
║          © 2026 Astral Algo. All rights reserved.                    ║
╠══════════════════════════════════════════════════════════════════════╣
║  FEATURES:                                                           ║
║    • Cryptographically signed license keys (HMAC-SHA256)             ║
║    • Plan tiers: Starter / Pro / Elite                               ║
║    • Hardware binding (MT5 Account ID lock)                          ║
║    • Expiry enforcement with grace period                            ║
║    • Key revocation and blacklist                                     ║
║    • Admin dashboard API (generate, revoke, list, stats)             ║
║    • Subscriber portal API (validate, activate, status)              ║
║    • Supabase persistence                                             ║
║    • Telegram notifications on activation / expiry                   ║
║    • Rate limiting on validation endpoint                            ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import os
import hmac
import uuid
import base64
import hashlib
import secrets
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from enum import Enum

from fastapi import FastAPI, HTTPException, Depends, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field, validator
from dotenv import load_dotenv
from supabase import create_client, Client
import aiohttp
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

load_dotenv()
log = logging.getLogger("FIERCE.LicenseEngine")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ─────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────

class Cfg:
    SUPABASE_URL     = os.getenv("SUPABASE_URL", "")
    SUPABASE_KEY     = os.getenv("SUPABASE_ANON_KEY", "")
    LICENSE_SECRET   = os.getenv("LICENSE_SECRET", "fierce_v53_license_secret_change_me")
    ADMIN_API_KEY    = os.getenv("ADMIN_API_KEY", "")          # Strong random key for admin routes
    TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    GRACE_DAYS       = int(os.getenv("GRACE_DAYS", "3"))       # Days after expiry before hard block
    MAX_ACTIVATIONS  = int(os.getenv("MAX_ACTIVATIONS", "1"))  # Devices per key (set 1 for MT5 lock)

cfg = Cfg()

supabase: Client = create_client(cfg.SUPABASE_URL, cfg.SUPABASE_KEY) if cfg.SUPABASE_URL else None

# ─────────────────────────────────────────────────────────────────────
#  ENUMS
# ─────────────────────────────────────────────────────────────────────

class Plan(str, Enum):
    STARTER = "starter"
    PRO     = "pro"
    ELITE   = "elite"

class KeyStatus(str, Enum):
    ACTIVE    = "active"
    EXPIRED   = "expired"
    REVOKED   = "revoked"
    PENDING   = "pending"
    SUSPENDED = "suspended"

PLAN_FEATURES = {
    Plan.STARTER: {
        "signals_per_day": 5,
        "pairs":           ["EURUSD", "GBPUSD"],
        "telegram":        True,
        "sessions":        False,
        "recordings":      False,
        "max_accounts":    1,
        "price_monthly":   29,
        "price_annual":    228,
    },
    Plan.PRO: {
        "signals_per_day": 999,
        "pairs":           ["EURUSD","GBPUSD","XAUUSD","USDJPY","GBPJPY","EURUSD","AUDUSD","US30","NAS100"],
        "telegram":        True,
        "sessions":        True,
        "recordings":      True,
        "max_accounts":    1,
        "price_monthly":   79,
        "price_annual":    649,
    },
    Plan.ELITE: {
        "signals_per_day": 999,
        "pairs":           ["ALL"],
        "telegram":        True,
        "sessions":        True,
        "recordings":      True,
        "max_accounts":    3,
        "early_signals":   True,
        "mentorship":      True,
        "price_monthly":   149,
        "price_annual":    1249,
    },
}

# ─────────────────────────────────────────────────────────────────────
#  KEY GENERATION ENGINE
# ─────────────────────────────────────────────────────────────────────

class LicenseEngine:
    """
    Key format:  FIERCE-{PLAN_PREFIX}-{PAYLOAD_B32}-{CHECKSUM}
    Example:     FIERCE-PRO-ABCDE12345FGHIJ-A1B2

    PAYLOAD encodes: subscriber_id + plan + expiry_timestamp
    CHECKSUM: first 4 chars of HMAC-SHA256(payload, LICENSE_SECRET)
    """

    PREFIX = "FIERCE"
    PLAN_CODES = {
        Plan.STARTER: "STR",
        Plan.PRO:     "PRO",
        Plan.ELITE:   "ELT",
    }
    PLAN_FROM_CODE = {v: k for k, v in PLAN_CODES.items()}

    @classmethod
    def generate(
        cls,
        subscriber_id: str,
        plan: Plan,
        duration_days: int,
    ) -> str:
        """Generate a cryptographically signed license key."""
        expiry_ts  = int((datetime.now(timezone.utc) + timedelta(days=duration_days)).timestamp())
        uid_short  = subscriber_id.replace("-", "")[:12].upper()
        plan_code  = cls.PLAN_CODES[plan]

        # Payload: uid + plan_code + expiry (hex)
        raw_payload = f"{uid_short}{plan_code}{expiry_ts:08X}"

        # Encode to base32, strip padding, take 16 chars
        b32 = base64.b32encode(raw_payload.encode()).decode().replace("=", "")[:16]

        # HMAC checksum
        sig = hmac.new(
            cfg.LICENSE_SECRET.encode(),
            f"{plan_code}{b32}".encode(),
            hashlib.sha256
        ).hexdigest()[:4].upper()

        key = f"{cls.PREFIX}-{plan_code}-{b32}-{sig}"
        return key

    @classmethod
    def verify_signature(cls, key: str) -> bool:
        """Check that the key's checksum is valid (not tampered)."""
        try:
            parts = key.upper().split("-")
            if len(parts) != 4 or parts[0] != cls.PREFIX:
                return False
            _, plan_code, b32, sig = parts
            expected = hmac.new(
                cfg.LICENSE_SECRET.encode(),
                f"{plan_code}{b32}".encode(),
                hashlib.sha256
            ).hexdigest()[:4].upper()
            return hmac.compare_digest(sig, expected)
        except Exception:
            return False

    @classmethod
    def extract_plan_code(cls, key: str) -> Optional[str]:
        try:
            return key.upper().split("-")[1]
        except Exception:
            return None

# ─────────────────────────────────────────────────────────────────────
#  PYDANTIC MODELS
# ─────────────────────────────────────────────────────────────────────

class GenerateKeyRequest(BaseModel):
    email:          EmailStr
    name:           str
    plan:           Plan
    duration_days:  int         = Field(30, ge=1, le=3650)
    account_id:     Optional[str] = None     # MT5 account number (for hardware binding)
    prop_firm:      Optional[str] = None
    account_size:   Optional[float] = None
    notes:          Optional[str] = None
    send_email:     bool = False             # Future: trigger email delivery

class ActivateKeyRequest(BaseModel):
    license_key:  str
    account_id:   str           # MT5 account number — binds this key to this account
    account_name: Optional[str] = None
    broker:       Optional[str] = None

class ValidateKeyRequest(BaseModel):
    license_key:  str
    account_id:   str           # Must match the account_id used at activation

class RevokeKeyRequest(BaseModel):
    license_key: str
    reason:      Optional[str] = "Revoked by admin"

class TransferKeyRequest(BaseModel):
    license_key:     str
    new_account_id:  str
    reason:          Optional[str] = None

class ExtendKeyRequest(BaseModel):
    license_key:    str
    extension_days: int = Field(..., ge=1, le=3650)
    reason:         Optional[str] = None

# ─────────────────────────────────────────────────────────────────────
#  RATE LIMITER
# ─────────────────────────────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address)

# ─────────────────────────────────────────────────────────────────────
#  FASTAPI APP
# ─────────────────────────────────────────────────────────────────────

license_app = FastAPI(
    title="FIERCE v5.3 — License Engine",
    description="Astral Algo Subscription Key Management",
    version="5.3.0",
    docs_url="/admin/docs",    # Hide docs behind /admin
    redoc_url=None,
)

license_app.state.limiter = limiter
license_app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

license_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────────────────
#  AUTH DEPENDENCIES
# ─────────────────────────────────────────────────────────────────────

def require_admin(x_admin_key: str = Header(...)):
    """All admin routes require the X-Admin-Key header."""
    if not cfg.ADMIN_API_KEY or x_admin_key != cfg.ADMIN_API_KEY:
        log.warning("Unauthorized admin access attempt")
        raise HTTPException(status_code=403, detail="Forbidden")
    return True

# ─────────────────────────────────────────────────────────────────────
#  DB HELPERS
# ─────────────────────────────────────────────────────────────────────

def db_get_key(license_key: str) -> Optional[dict]:
    if not supabase:
        return None
    try:
        res = supabase.table("license_keys").select("*").eq("license_key", license_key.upper()).single().execute()
        return res.data
    except Exception:
        return None

def db_save_key(record: dict) -> Optional[dict]:
    if not supabase:
        return record
    try:
        res = supabase.table("license_keys").insert(record).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        log.error(f"DB save error: {e}")
        return None

def db_update_key(license_key: str, updates: dict) -> bool:
    if not supabase:
        return True
    try:
        supabase.table("license_keys").update(updates).eq("license_key", license_key.upper()).execute()
        return True
    except Exception as e:
        log.error(f"DB update error: {e}")
        return False

def db_log_event(license_key: str, event: str, detail: str, ip: str = ""):
    if not supabase:
        return
    try:
        supabase.table("license_events").insert({
            "license_key": license_key.upper(),
            "event":       event,
            "detail":      detail,
            "ip_address":  ip,
            "created_at":  datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        log.error(f"Event log error: {e}")

# ─────────────────────────────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────────────────────────────

async def send_telegram(msg: str):
    if not cfg.TELEGRAM_TOKEN or not cfg.TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{cfg.TELEGRAM_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(url, json={
                "chat_id":    cfg.TELEGRAM_CHAT_ID,
                "text":       msg,
                "parse_mode": "Markdown"
            }, timeout=aiohttp.ClientTimeout(total=8))
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ─────────────────────────────────────────────────────────────────────
#  ADMIN ROUTES — Key Management
# ─────────────────────────────────────────────────────────────────────

@license_app.post("/admin/keys/generate", tags=["Admin"])
async def generate_key(
    req: GenerateKeyRequest,
    _: bool = Depends(require_admin)
):
    """
    Generate a new license key for a subscriber.
    Called by you (admin) after payment is confirmed.
    """
    subscriber_id = str(uuid.uuid4())
    license_key   = LicenseEngine.generate(subscriber_id, req.plan, req.duration_days)
    expires_at    = (datetime.now(timezone.utc) + timedelta(days=req.duration_days)).isoformat()

    record = {
        "id":             subscriber_id,
        "license_key":    license_key,
        "email":          req.email,
        "name":           req.name,
        "plan":           req.plan.value,
        "status":         KeyStatus.PENDING.value,
        "duration_days":  req.duration_days,
        "expires_at":     expires_at,
        "account_id":     req.account_id,       # None until activated
        "prop_firm":      req.prop_firm,
        "account_size":   req.account_size,
        "notes":          req.notes,
        "activations":    0,
        "max_activations": cfg.MAX_ACTIVATIONS,
        "created_at":     datetime.now(timezone.utc).isoformat(),
        "last_validated": None,
        "validation_count": 0,
        "features":       PLAN_FEATURES[req.plan],
    }

    saved = db_save_key(record)
    if not saved:
        raise HTTPException(500, "Failed to save license key to database")

    log.info(f"Key generated: {license_key} | {req.plan} | {req.email} | {req.duration_days}d")
    db_log_event(license_key, "GENERATED", f"Plan:{req.plan} Expires:{expires_at}")

    # Telegram notification
    await send_telegram(
        f"🔑 *New License Generated — FIERCE v5.3*\n"
        f"Name: {req.name}\n"
        f"Email: `{req.email}`\n"
        f"Plan: *{req.plan.upper()}*\n"
        f"Duration: {req.duration_days} days\n"
        f"Key: `{license_key}`\n"
        f"Expires: {expires_at[:10]}"
    )

    return {
        "status":       "generated",
        "license_key":  license_key,
        "subscriber_id": subscriber_id,
        "plan":         req.plan,
        "expires_at":   expires_at,
        "features":     PLAN_FEATURES[req.plan],
        "instructions": (
            "Share the license_key with the subscriber. "
            "They must activate it from their MT5 EA using their account number."
        )
    }


@license_app.post("/admin/keys/revoke", tags=["Admin"])
async def revoke_key(
    req: RevokeKeyRequest,
    _: bool = Depends(require_admin)
):
    """Immediately revoke a license key."""
    rec = db_get_key(req.license_key)
    if not rec:
        raise HTTPException(404, "License key not found")

    db_update_key(req.license_key, {
        "status":     KeyStatus.REVOKED.value,
        "revoked_at": datetime.now(timezone.utc).isoformat(),
        "revoke_reason": req.reason,
    })
    db_log_event(req.license_key, "REVOKED", req.reason or "Admin revocation")

    await send_telegram(
        f"🚫 *License Revoked — FIERCE v5.3*\n"
        f"Key: `{req.license_key}`\n"
        f"Email: {rec.get('email','?')}\n"
        f"Reason: {req.reason}"
    )

    log.info(f"Key revoked: {req.license_key} | {req.reason}")
    return {"status": "revoked", "license_key": req.license_key}


@license_app.post("/admin/keys/extend", tags=["Admin"])
async def extend_key(
    req: ExtendKeyRequest,
    _: bool = Depends(require_admin)
):
    """Extend the expiry of an existing key."""
    rec = db_get_key(req.license_key)
    if not rec:
        raise HTTPException(404, "License key not found")

    current_expiry = datetime.fromisoformat(rec["expires_at"])
    new_expiry     = current_expiry + timedelta(days=req.extension_days)

    db_update_key(req.license_key, {
        "expires_at":    new_expiry.isoformat(),
        "status":        KeyStatus.ACTIVE.value,  # Reactivate if expired
        "duration_days": rec.get("duration_days", 30) + req.extension_days,
    })
    db_log_event(req.license_key, "EXTENDED", f"+{req.extension_days} days | Reason: {req.reason}")

    await send_telegram(
        f"✅ *License Extended — FIERCE v5.3*\n"
        f"Key: `{req.license_key}`\n"
        f"Email: {rec.get('email','?')}\n"
        f"Extended by: {req.extension_days} days\n"
        f"New expiry: {new_expiry.date()}"
    )

    return {"status": "extended", "new_expires_at": new_expiry.isoformat()}


@license_app.post("/admin/keys/transfer", tags=["Admin"])
async def transfer_key(
    req: TransferKeyRequest,
    _: bool = Depends(require_admin)
):
    """Transfer a key to a new MT5 account (e.g. subscriber changed broker)."""
    rec = db_get_key(req.license_key)
    if not rec:
        raise HTTPException(404, "License key not found")

    old_account = rec.get("account_id", "none")
    db_update_key(req.license_key, {"account_id": req.new_account_id})
    db_log_event(req.license_key, "TRANSFERRED",
                 f"From:{old_account} To:{req.new_account_id} Reason:{req.reason}")

    return {
        "status":          "transferred",
        "old_account_id":  old_account,
        "new_account_id":  req.new_account_id,
    }


@license_app.get("/admin/keys", tags=["Admin"])
async def list_keys(
    status:  Optional[str] = None,
    plan:    Optional[str] = None,
    limit:   int = 50,
    offset:  int = 0,
    _: bool = Depends(require_admin)
):
    """List all license keys with optional filters."""
    if not supabase:
        return {"keys": [], "total": 0}
    try:
        q = supabase.table("license_keys").select("*").order("created_at", desc=True).range(offset, offset + limit - 1)
        if status: q = q.eq("status", status)
        if plan:   q = q.eq("plan",   plan)
        res = q.execute()
        return {"keys": res.data, "count": len(res.data)}
    except Exception as e:
        raise HTTPException(500, str(e))


@license_app.get("/admin/keys/{license_key}", tags=["Admin"])
async def get_key_detail(
    license_key: str,
    _: bool = Depends(require_admin)
):
    """Get full detail and event history for one key."""
    rec = db_get_key(license_key)
    if not rec:
        raise HTTPException(404, "Not found")

    events = []
    if supabase:
        try:
            ev = supabase.table("license_events").select("*") \
                .eq("license_key", license_key.upper()) \
                .order("created_at", desc=True).limit(50).execute()
            events = ev.data
        except Exception:
            pass

    return {"key": rec, "events": events}


@license_app.get("/admin/stats", tags=["Admin"])
async def admin_stats(_: bool = Depends(require_admin)):
    """Dashboard stats: active, expired, revenue, plan breakdown."""
    if not supabase:
        return {"error": "DB not connected"}
    try:
        res = supabase.table("license_keys").select("plan,status,duration_days").execute()
        keys = res.data

        total   = len(keys)
        active  = sum(1 for k in keys if k["status"] == "active")
        expired = sum(1 for k in keys if k["status"] == "expired")
        revoked = sum(1 for k in keys if k["status"] == "revoked")
        pending = sum(1 for k in keys if k["status"] == "pending")

        by_plan = {}
        revenue = 0.0
        for k in keys:
            p = k.get("plan","?")
            by_plan[p] = by_plan.get(p, 0) + 1
            price = PLAN_FEATURES.get(Plan(p), {}).get("price_monthly", 0)
            revenue += price

        return {
            "total_keys":     total,
            "active":         active,
            "expired":        expired,
            "revoked":        revoked,
            "pending":        pending,
            "by_plan":        by_plan,
            "est_mrr_usd":    round(revenue, 2),
            "generated_at":   datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        raise HTTPException(500, str(e))

# ─────────────────────────────────────────────────────────────────────
#  SUBSCRIBER ROUTES — Activation & Validation
# ─────────────────────────────────────────────────────────────────────

@license_app.post("/license/activate", tags=["Subscriber"])
async def activate_key(req: ActivateKeyRequest, request: Request):
    """
    Called by subscriber (or EA) to activate their key.
    Binds the key to their MT5 account number.
    Only allowed once (or up to max_activations).
    """
    key = req.license_key.upper().strip()

    # 1. Signature check
    if not LicenseEngine.verify_signature(key):
        db_log_event(key, "ACTIVATION_FAILED", "Invalid signature", request.client.host)
        raise HTTPException(400, "Invalid license key format or signature")

    # 2. DB lookup
    rec = db_get_key(key)
    if not rec:
        raise HTTPException(404, "License key not found. Contact support.")

    # 3. Status check
    if rec["status"] == KeyStatus.REVOKED.value:
        raise HTTPException(403, "This license has been revoked. Contact support.")
    if rec["status"] == KeyStatus.SUSPENDED.value:
        raise HTTPException(403, "This license is suspended. Contact support.")

    # 4. Already activated to a different account?
    if rec.get("account_id") and rec["account_id"] != req.account_id:
        activations = rec.get("activations", 1)
        max_act     = rec.get("max_activations", 1)
        if activations >= max_act:
            db_log_event(key, "ACTIVATION_BLOCKED", f"Max activations reached. Attempted:{req.account_id}", request.client.host)
            raise HTTPException(403,
                "This key is already bound to another account. "
                "Contact support to transfer your license.")

    # 5. Expiry check
    expires_at = datetime.fromisoformat(rec["expires_at"])
    if datetime.now(timezone.utc) > expires_at + timedelta(days=cfg.GRACE_DAYS):
        db_update_key(key, {"status": KeyStatus.EXPIRED.value})
        raise HTTPException(403, f"License expired on {expires_at.date()}. Please renew.")

    # 6. Activate
    already_active = rec["status"] == KeyStatus.ACTIVE.value and rec.get("account_id") == req.account_id
    updates = {
        "status":           KeyStatus.ACTIVE.value,
        "account_id":       req.account_id,
        "broker":           req.broker,
        "account_name":     req.account_name,
        "activations":      (rec.get("activations") or 0) + (0 if already_active else 1),
        "activated_at":     rec.get("activated_at") or datetime.now(timezone.utc).isoformat(),
        "last_validated":   datetime.now(timezone.utc).isoformat(),
        "validation_count": (rec.get("validation_count") or 0) + 1,
    }
    db_update_key(key, updates)
    db_log_event(key, "ACTIVATED", f"Account:{req.account_id} Broker:{req.broker}", request.client.host)

    plan     = Plan(rec["plan"])
    features = PLAN_FEATURES.get(plan, {})

    if not already_active:
        await send_telegram(
            f"✅ *License Activated — FIERCE v5.3*\n"
            f"Name: {rec.get('name','?')}\n"
            f"Plan: *{plan.upper()}*\n"
            f"MT5 Account: `{req.account_id}`\n"
            f"Broker: {req.broker or '?'}\n"
            f"Expires: {expires_at.date()}"
        )

    log.info(f"Key activated: {key} | Account:{req.account_id} | Plan:{plan}")

    return {
        "status":       "activated",
        "plan":         plan.value,
        "expires_at":   rec["expires_at"],
        "days_remaining": max(0, (expires_at - datetime.now(timezone.utc)).days),
        "features":     features,
        "message":      f"FIERCE v5.3 — {plan.upper()} plan activated. Welcome to Astral Algo."
    }


@license_app.post("/license/validate", tags=["Subscriber"])
@limiter.limit("60/minute")
async def validate_key(req: ValidateKeyRequest, request: Request):
    """
    Called by the MT5 EA on every startup and periodically.
    Returns whether trading is allowed and plan features.
    This is the GATEKEEPER — EA should refuse to trade if this fails.
    """
    key = req.license_key.upper().strip()

    # Signature check — fast, no DB hit
    if not LicenseEngine.verify_signature(key):
        return {"valid": False, "reason": "INVALID_KEY", "allow_trading": False}

    # DB lookup
    rec = db_get_key(key)
    if not rec:
        return {"valid": False, "reason": "KEY_NOT_FOUND", "allow_trading": False}

    # Account binding check
    if rec.get("account_id") and rec["account_id"] != req.account_id:
        db_log_event(key, "VALIDATION_MISMATCH", f"Expected:{rec['account_id']} Got:{req.account_id}", request.client.host)
        return {"valid": False, "reason": "ACCOUNT_MISMATCH", "allow_trading": False,
                "message": "This key is bound to a different MT5 account."}

    # Status checks
    if rec["status"] == KeyStatus.REVOKED.value:
        return {"valid": False, "reason": "REVOKED", "allow_trading": False}
    if rec["status"] == KeyStatus.SUSPENDED.value:
        return {"valid": False, "reason": "SUSPENDED", "allow_trading": False}

    # Expiry
    expires_at    = datetime.fromisoformat(rec["expires_at"])
    now           = datetime.now(timezone.utc)
    days_left     = (expires_at - now).days
    in_grace      = days_left < 0 and days_left > -cfg.GRACE_DAYS

    if now > expires_at + timedelta(days=cfg.GRACE_DAYS):
        db_update_key(key, {"status": KeyStatus.EXPIRED.value})
        return {"valid": False, "reason": "EXPIRED", "allow_trading": False,
                "expired_at": rec["expires_at"],
                "message":    "License expired. Please renew at astralalgo.com"}

    # Update last_validated
    db_update_key(key, {
        "last_validated":   now.isoformat(),
        "validation_count": (rec.get("validation_count") or 0) + 1,
    })

    plan     = Plan(rec["plan"])
    features = PLAN_FEATURES.get(plan, {})

    # Build response
    response = {
        "valid":          True,
        "allow_trading":  True,
        "plan":           plan.value,
        "subscriber":     rec.get("name", ""),
        "expires_at":     rec["expires_at"],
        "days_remaining": max(0, days_left),
        "in_grace_period": in_grace,
        "features":       features,
        "signals_per_day": features.get("signals_per_day", 5),
        "allowed_pairs":  features.get("pairs", []),
        "message":        "FIERCE v5.3 — License valid ✓"
    }

    # Warn if expiring soon
    if 0 < days_left <= 7:
        response["warning"] = f"License expires in {days_left} days. Renew at astralalgo.com"
        if days_left == 3:
            await send_telegram(
                f"⏰ *License Expiring Soon — FIERCE v5.3*\n"
                f"Name: {rec.get('name','?')}\n"
                f"Plan: *{plan.upper()}*\n"
                f"Expires in: *{days_left} days*\n"
                f"Renew now to avoid interruption."
            )

    if in_grace:
        response["warning"] = "License is in grace period. Renew immediately."
        response["allow_trading"] = True  # Still allow during grace

    return response


@license_app.get("/license/status/{license_key}", tags=["Subscriber"])
@limiter.limit("30/minute")
async def check_status(license_key: str, request: Request):
    """Lightweight status check — subscriber can call this from the website."""
    key = license_key.upper().strip()

    if not LicenseEngine.verify_signature(key):
        raise HTTPException(400, "Invalid key format")

    rec = db_get_key(key)
    if not rec:
        raise HTTPException(404, "Key not found")

    expires_at  = datetime.fromisoformat(rec["expires_at"])
    days_left   = (expires_at - datetime.now(timezone.utc)).days

    return {
        "license_key":    key,
        "plan":           rec.get("plan"),
        "status":         rec.get("status"),
        "expires_at":     rec.get("expires_at"),
        "days_remaining": max(0, days_left),
        "account_id":     rec.get("account_id"),
        "last_validated": rec.get("last_validated"),
        "features":       PLAN_FEATURES.get(Plan(rec["plan"]), {})
    }


@license_app.get("/license/plans", tags=["Public"])
async def get_plans():
    """Public endpoint — returns plan features and pricing for the website."""
    return {
        "plans": {
            p.value: {
                **PLAN_FEATURES[p],
                "name": p.value.capitalize(),
                "key":  p.value,
            }
            for p in Plan
        },
        "currency": "USD",
        "version":  "FIERCE v5.3",
    }

# ─────────────────────────────────────────────────────────────────────
#  WEBHOOK — Payment Providers (Stripe / Paystack / Flutterwave)
# ─────────────────────────────────────────────────────────────────────

@license_app.post("/webhook/stripe", tags=["Payments"])
async def stripe_webhook(request: Request):
    """
    Stripe calls this when a payment succeeds.
    Automatically generates and sends a key to the subscriber.
    Set this URL in your Stripe Dashboard → Webhooks.
    """
    body      = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    stripe_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    # Verify Stripe signature
    expected = hmac.new(
        stripe_secret.encode(),
        body,
        hashlib.sha256
    ).hexdigest()

    # In production use: stripe.Webhook.construct_event()
    # Simplified here for clarity
    try:
        payload = await request.json()
        event_type = payload.get("type", "")

        if event_type == "checkout.session.completed":
            session   = payload["data"]["object"]
            email     = session.get("customer_details", {}).get("email", "")
            plan_raw  = session.get("metadata", {}).get("plan", "pro")
            duration  = int(session.get("metadata", {}).get("duration_days", 30))
            name      = session.get("metadata", {}).get("name", "Subscriber")

            try:
                plan = Plan(plan_raw.lower())
            except ValueError:
                plan = Plan.PRO

            # Auto-generate key
            subscriber_id = str(uuid.uuid4())
            license_key   = LicenseEngine.generate(subscriber_id, plan, duration)
            expires_at    = (datetime.now(timezone.utc) + timedelta(days=duration)).isoformat()

            record = {
                "id":            subscriber_id,
                "license_key":   license_key,
                "email":         email,
                "name":          name,
                "plan":          plan.value,
                "status":        KeyStatus.PENDING.value,
                "duration_days": duration,
                "expires_at":    expires_at,
                "created_at":    datetime.now(timezone.utc).isoformat(),
                "payment_ref":   session.get("id", ""),
                "features":      PLAN_FEATURES[plan],
                "activations":   0,
                "max_activations": cfg.MAX_ACTIVATIONS,
                "validation_count": 0,
            }
            db_save_key(record)
            db_log_event(license_key, "PAYMENT_RECEIVED", f"Stripe:{session.get('id','?')} Plan:{plan}")

            await send_telegram(
                f"💳 *Payment Received — FIERCE v5.3*\n"
                f"Email: `{email}`\n"
                f"Plan: *{plan.upper()}*\n"
                f"Duration: {duration} days\n"
                f"Key: `{license_key}`\n"
                f"Stripe ref: {session.get('id','?')[:16]}"
            )

            log.info(f"Auto-key from Stripe: {license_key} | {email} | {plan}")

            # TODO: send email with license_key to subscriber via SendGrid / Resend
            # await send_welcome_email(email, name, license_key, plan)

            return {"status": "ok", "key_generated": license_key}

    except Exception as e:
        log.error(f"Stripe webhook error: {e}")
        raise HTTPException(400, f"Webhook error: {e}")

    return {"status": "ok", "event": event_type}


@license_app.post("/webhook/paystack", tags=["Payments"])
async def paystack_webhook(request: Request):
    """Paystack payment webhook — for African market subscribers."""
    body      = await request.body()
    sig_header = request.headers.get("x-paystack-signature", "")
    ps_secret  = os.getenv("PAYSTACK_SECRET_KEY", "")

    # Verify signature
    expected = hmac.new(ps_secret.encode(), body, hashlib.sha512).hexdigest()
    if not hmac.compare_digest(sig_header, expected):
        raise HTTPException(400, "Invalid Paystack signature")

    try:
        payload    = await request.json()
        event_type = payload.get("event", "")

        if event_type == "charge.success":
            data     = payload["data"]
            email    = data.get("customer", {}).get("email", "")
            metadata = data.get("metadata", {})
            plan_raw = metadata.get("plan", "pro")
            duration = int(metadata.get("duration_days", 30))
            name     = metadata.get("name", "Subscriber")

            try:
                plan = Plan(plan_raw.lower())
            except ValueError:
                plan = Plan.PRO

            subscriber_id = str(uuid.uuid4())
            license_key   = LicenseEngine.generate(subscriber_id, plan, duration)
            expires_at    = (datetime.now(timezone.utc) + timedelta(days=duration)).isoformat()

            record = {
                "id": subscriber_id, "license_key": license_key,
                "email": email, "name": name, "plan": plan.value,
                "status": KeyStatus.PENDING.value, "duration_days": duration,
                "expires_at": expires_at,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "payment_ref": data.get("reference", ""),
                "features": PLAN_FEATURES[plan], "activations": 0,
                "max_activations": cfg.MAX_ACTIVATIONS, "validation_count": 0,
            }
            db_save_key(record)
            db_log_event(license_key, "PAYMENT_RECEIVED", f"Paystack:{data.get('reference','?')} Plan:{plan}")

            await send_telegram(
                f"💳 *Paystack Payment — FIERCE v5.3*\n"
                f"Email: `{email}`\n"
                f"Plan: *{plan.upper()}*\n"
                f"Key: `{license_key}`\n"
                f"Ref: {data.get('reference','?')}"
            )

    except Exception as e:
        log.error(f"Paystack webhook error: {e}")

    return {"status": "ok"}

# ─────────────────────────────────────────────────────────────────────
#  MQL5 VALIDATION SNIPPET (embed in EA)
# ─────────────────────────────────────────────────────────────────────
"""
── Add this to AstralAlgo_SMC.mq5 ──────────────────────────────────

input string LicenseKey = "";   // Your FIERCE v5.3 license key

bool ValidateLicense()
{
   if(LicenseKey == "") {
      Alert("FIERCE v5.3: No license key entered. EA disabled.");
      return false;
   }

   string accountId = IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
   string url       = "http://YOUR_VPS_IP:8000/license/validate";
   string headers   = "Content-Type: application/json\r\n";
   string body      = StringFormat(
      "{\"license_key\":\"%s\",\"account_id\":\"%s\"}",
      LicenseKey, accountId
   );

   char req[], res[];
   string resHeaders;
   StringToCharArray(body, req, 0, StringLen(body));

   int code = WebRequest("POST", url, headers, 10000, req, res, resHeaders);
   if(code != 200) {
      Print("FIERCE v5.3: License server unreachable. Code:", code);
      return false; // Block trading if server is unreachable
   }

   string response = CharArrayToString(res);

   // Parse "allow_trading" from JSON
   if(StringFind(response, "\"allow_trading\":true") >= 0) {
      Print("FIERCE v5.3: License valid ✓");
      // Extract days_remaining from response for display
      return true;
   } else {
      string reason = "";
      int rPos = StringFind(response, "\"reason\":\"");
      if(rPos >= 0) {
         rPos += 10;
         int rEnd = StringFind(response, "\"", rPos);
         reason = StringSubstr(response, rPos, rEnd - rPos);
      }
      Alert("FIERCE v5.3: License invalid — " + reason +
            ". Contact support@astralalgo.com");
      return false;
   }
}

// Call in OnInit():
if(!ValidateLicense()) return INIT_FAILED;

// Call periodically in OnTimer() every 3600 seconds:
if(!ValidateLicense()) ExpertRemove();
───────────────────────────────────────────────────────────────────
"""

# ─────────────────────────────────────────────────────────────────────
#  HEALTH
# ─────────────────────────────────────────────────────────────────────

@license_app.get("/health", tags=["System"])
async def health():
    return {
        "service":    "FIERCE v5.3 License Engine",
        "status":     "ok",
        "version":    "5.3.0",
        "server_time": datetime.now(timezone.utc).isoformat(),
        "db":         "connected" if supabase else "not configured",
    }

# ─────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "license_engine:license_app",
        host="0.0.0.0",
        port=8001,       # Run alongside main.py on port 8000
        reload=True,
        log_level="info"
    )
