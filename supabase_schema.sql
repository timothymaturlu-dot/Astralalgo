-- ═══════════════════════════════════════════════════════════════════
--  ASTRAL ALGO — SUPABASE SCHEMA
--  Run this in your Supabase SQL Editor to set up all tables
--  Dashboard: https://app.supabase.com → SQL Editor → New Query
-- ═══════════════════════════════════════════════════════════════════

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";


-- ── TABLE: signals ───────────────────────────────────────────────────
-- Stores every signal fired by the MT5 EA

CREATE TABLE IF NOT EXISTS signals (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    symbol          TEXT        NOT NULL,
    direction       TEXT        NOT NULL CHECK (direction IN ('BUY','SELL')),
    entry           NUMERIC     NOT NULL,
    sl              NUMERIC     NOT NULL,
    tp1             NUMERIC     NOT NULL,
    tp2             NUMERIC,
    tp3             NUMERIC,
    lots            NUMERIC,
    rr              NUMERIC,
    confidence      NUMERIC,
    session         TEXT,
    confluences     TEXT,
    magic           BIGINT,
    account_size    NUMERIC,
    risk_pct        NUMERIC,
    propfirm_mode   BOOLEAN     DEFAULT TRUE,
    status          TEXT        DEFAULT 'ACTIVE'
                    CHECK (status IN ('ACTIVE','TP1_HIT','TP2_HIT','TP3_HIT','SL_HIT','PARTIAL','CLOSED')),
    pips_gained     NUMERIC     DEFAULT 0,
    pnl_usd         NUMERIC     DEFAULT 0,
    exit_price      NUMERIC,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    closed_at       TIMESTAMPTZ
);

-- Indexes for fast dashboard queries
CREATE INDEX IF NOT EXISTS idx_signals_created  ON signals (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_symbol   ON signals (symbol);
CREATE INDEX IF NOT EXISTS idx_signals_status   ON signals (status);
CREATE INDEX IF NOT EXISTS idx_signals_direction ON signals (direction);


-- ── TABLE: account_snapshots ─────────────────────────────────────────
-- Periodic account state snapshots from the EA

CREATE TABLE IF NOT EXISTS account_snapshots (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    magic           BIGINT,
    balance         NUMERIC,
    equity          NUMERIC,
    margin          NUMERIC,
    free_margin     NUMERIC,
    daily_dd_pct    NUMERIC,
    total_dd_pct    NUMERIC,
    open_trades     INTEGER,
    daily_pnl_usd   NUMERIC,
    daily_pnl_pct   NUMERIC,
    server_time     TEXT,
    recorded_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_acc_recorded ON account_snapshots (recorded_at DESC);


-- ── TABLE: subscribers ───────────────────────────────────────────────
-- Subscription/member management

CREATE TABLE IF NOT EXISTS subscribers (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email           TEXT        UNIQUE NOT NULL,
    name            TEXT,
    telegram_id     TEXT,
    plan            TEXT        DEFAULT 'starter'
                    CHECK (plan IN ('starter','pro','elite')),
    status          TEXT        DEFAULT 'active'
                    CHECK (status IN ('active','inactive','trial','suspended')),
    account_size    NUMERIC,
    prop_firm       TEXT,
    subscribed_at   TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    signals_received INTEGER    DEFAULT 0,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_subs_email  ON subscribers (email);
CREATE INDEX IF NOT EXISTS idx_subs_plan   ON subscribers (plan);
CREATE INDEX IF NOT EXISTS idx_subs_status ON subscribers (status);


-- ── TABLE: sessions ──────────────────────────────────────────────────
-- Meeting/trading sessions scheduled on the platform

CREATE TABLE IF NOT EXISTS sessions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    title           TEXT        NOT NULL,
    description     TEXT,
    session_type    TEXT        CHECK (session_type IN ('live','class','recording','qa')),
    host            TEXT        DEFAULT 'Astral Host',
    starts_at       TIMESTAMPTZ,
    duration_min    INTEGER,
    recording_url   TEXT,
    attendees       INTEGER     DEFAULT 0,
    status          TEXT        DEFAULT 'upcoming'
                    CHECK (status IN ('upcoming','live','completed','cancelled')),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);


-- ── TABLE: journal ───────────────────────────────────────────────────
-- Post-trade analysis journal (manual + auto entries)

CREATE TABLE IF NOT EXISTS journal (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    signal_id       UUID REFERENCES signals(id),
    symbol          TEXT,
    direction       TEXT,
    entry           NUMERIC,
    exit_price      NUMERIC,
    pips            NUMERIC,
    pnl_usd         NUMERIC,
    rr_achieved     NUMERIC,
    setup_quality   INTEGER     CHECK (setup_quality BETWEEN 1 AND 5),
    mistakes        TEXT,
    notes           TEXT,
    screenshot_url  TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);


-- ── ROW LEVEL SECURITY ───────────────────────────────────────────────
-- Enable RLS on subscriber-sensitive tables

ALTER TABLE subscribers ENABLE ROW LEVEL SECURITY;
ALTER TABLE journal     ENABLE ROW LEVEL SECURITY;

-- Public read for signals and sessions (website dashboard)
ALTER TABLE signals         ENABLE ROW LEVEL SECURITY;
ALTER TABLE account_snapshots ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Public read signals" ON signals
    FOR SELECT USING (true);

CREATE POLICY "Service role insert signals" ON signals
    FOR INSERT WITH CHECK (true);

CREATE POLICY "Service role update signals" ON signals
    FOR UPDATE USING (true);

CREATE POLICY "Public read sessions" ON sessions
    FOR SELECT USING (true);

-- Only service role can read account/subscriber data
CREATE POLICY "Service only account" ON account_snapshots
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "Service only subscribers" ON subscribers
    FOR ALL USING (auth.role() = 'service_role');


-- ── SAMPLE DATA (optional — remove in production) ────────────────────

INSERT INTO signals (symbol, direction, entry, sl, tp1, tp2, tp3, lots, rr, confidence, session, confluences, status, pips_gained, pnl_usd)
VALUES
    ('XAUUSD', 'BUY',  2341.50, 2332.00, 2354.00, 2368.00, 2385.00, 0.25, 2.4, 92, 'NY Kill Zone',    'HTF Bias ✓ | MSB ✓ | Liq Sweep ✓ | OB ✓ | FVG ✓', 'ACTIVE',  82,   164),
    ('GBPUSD', 'BUY',  1.2720,  1.2681,  1.2778,  1.2830,  1.2900,  0.10, 2.1, 87, 'London KZ',       'HTF Bias ✓ | OTE ✓ | OB ✓ | CISD ✓',               'ACTIVE',  58,   58),
    ('EURUSD', 'SELL', 1.0821,  1.0860,  1.0768,  1.0720,  1.0660,  0.10, 2.6, 89, 'London KZ',       'HTF Bias ✓ | MSB ✓ | Liq Sweep ✓ | OB ✓',           'TP1_HIT', 53,   53),
    ('USDJPY', 'SELL', 154.80,  155.40,  153.90,  153.10,  152.00,  0.05, 2.8, 78, 'Tokyo KZ',        'HTF Bias ✓ | OTE ✓ | FVG ✓',                        'ACTIVE',  90,   45),
    ('US30',   'BUY',  39650,   39520,   39820,   39990,   40200,   0.02, 2.1, 83, 'NY Kill Zone',     'HTF Bias ✓ | MSB ✓ | OB ✓',                         'TP2_HIT', 170,  340);

INSERT INTO sessions (title, session_type, host, starts_at, duration_min, attendees, status)
VALUES
    ('NY Session Live Trade — XAUUSD & GBPUSD', 'live',      'Astral Host', NOW(),                    90,  142, 'live'),
    ('Prop Firm Phase 1 Masterclass',           'class',     'Astral Host', NOW() + INTERVAL '6 hours', 60, 67,  'upcoming'),
    ('London Open Breakout Strategy',           'recording', 'Astral Host', NOW() - INTERVAL '3 days',  72,  0,  'completed');

-- ═══════════════════════════════════════════════════════════════════
--  END OF SCHEMA
-- ═══════════════════════════════════════════════════════════════════


-- ═══════════════════════════════════════════════════════════════════
--  FIERCE v5.3 — LICENSE ENGINE TABLES
--  Add these to your existing Supabase schema
-- ═══════════════════════════════════════════════════════════════════

-- ── TABLE: license_keys ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS license_keys (
    id                UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    license_key       TEXT        UNIQUE NOT NULL,
    email             TEXT        NOT NULL,
    name              TEXT,
    plan              TEXT        NOT NULL CHECK (plan IN ('starter','pro','elite')),
    status            TEXT        DEFAULT 'pending'
                      CHECK (status IN ('active','expired','revoked','pending','suspended')),
    duration_days     INTEGER     DEFAULT 30,
    expires_at        TIMESTAMPTZ NOT NULL,
    activated_at      TIMESTAMPTZ,
    revoked_at        TIMESTAMPTZ,
    revoke_reason     TEXT,
    account_id        TEXT,
    account_name      TEXT,
    broker            TEXT,
    prop_firm         TEXT,
    account_size      NUMERIC,
    activations       INTEGER     DEFAULT 0,
    max_activations   INTEGER     DEFAULT 1,
    validation_count  INTEGER     DEFAULT 0,
    last_validated    TIMESTAMPTZ,
    payment_ref       TEXT,
    notes             TEXT,
    features          JSONB,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lk_key     ON license_keys (license_key);
CREATE INDEX IF NOT EXISTS idx_lk_email   ON license_keys (email);
CREATE INDEX IF NOT EXISTS idx_lk_status  ON license_keys (status);
CREATE INDEX IF NOT EXISTS idx_lk_plan    ON license_keys (plan);
CREATE INDEX IF NOT EXISTS idx_lk_account ON license_keys (account_id);

-- ── TABLE: license_events ─────────────────────────────────────────────
-- Full audit trail of every key action
CREATE TABLE IF NOT EXISTS license_events (
    id           UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    license_key  TEXT        NOT NULL,
    event        TEXT        NOT NULL,
    detail       TEXT,
    ip_address   TEXT,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_le_key     ON license_events (license_key);
CREATE INDEX IF NOT EXISTS idx_le_event   ON license_events (event);
CREATE INDEX IF NOT EXISTS idx_le_created ON license_events (created_at DESC);

-- RLS
ALTER TABLE license_keys   ENABLE ROW LEVEL SECURITY;
ALTER TABLE license_events ENABLE ROW LEVEL SECURITY;

-- Only service role can read/write license data
CREATE POLICY "Service only license_keys"   ON license_keys   FOR ALL USING (auth.role() = 'service_role');
CREATE POLICY "Service only license_events" ON license_events FOR ALL USING (auth.role() = 'service_role');

