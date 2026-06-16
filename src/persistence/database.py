# =============================================================================
# R2R INTERCOMPANY RECONCILIATION ENGINE
# src/persistence/database.py
#
# What this file does:
#   Manages the database connection and initialisation.
#   Connects to Supabase PostgreSQL via Supavisor pooler (port 6543).
#   Falls back to SQLite for local development when Supabase is not configured.
#   Creates all tables on first run.
#   Creates the SOX audit ledger PostgreSQL trigger.
#   Creates the period lock PostgreSQL trigger.
#
#   Connection design:
#     Supabase requires connection via Supavisor on port 6543 (NOT 5432).
#     GitHub Actions runners use IPv4 — Supabase default endpoint is IPv6.
#     Supavisor resolves to IPv4 and acts as connection pooler.
#     pool_pre_ping=True handles Supabase's connection lifecycle.
#     sslmode=require enforces TLS for financial data in transit.
#
#   SQLite fallback:
#     When SUPABASE_POOLER_URL is not set (local development),
#     the system automatically uses SQLite.
#     SQLite does not support PostgreSQL triggers —
#     the SOX trigger and period lock are skipped on SQLite.
#     Set database.sqlite_fallback.enabled=false in config.yaml
#     once Supabase is configured.
#
# How other files use it:
#   from src.persistence.database import get_engine, get_session, init_db
#   engine = get_engine()
#   with get_session() as session:
#       session.add(some_model)
#       session.commit()
# =============================================================================

import os
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, text, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, Session

from src.config import config
from src.persistence.models import Base, ALL_MODELS


# -----------------------------------------------------------------------------
# DATABASE URL BUILDER
# Selects Supabase or SQLite based on environment and config
# -----------------------------------------------------------------------------

def _build_database_url() -> tuple[str, bool]:
    """
    Builds the database connection URL.

    Priority:
    1. SUPABASE_POOLER_URL environment variable (production)
    2. SQLite fallback (development)

    Returns:
        (database_url, is_sqlite)
    """
    supabase_url = os.environ.get("SUPABASE_POOLER_URL", "").strip()

    if supabase_url:
        # Production: Supabase via Supavisor IPv4 pooler
        # URL format: postgresql://postgres.[ref]:[pass]@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres
        if ":5432" in supabase_url:
            print(
                "[DATABASE] WARNING: Connection URL uses port 5432. "
                "Supabase requires port 6543 (Supavisor pooler) for "
                "GitHub Actions IPv4 compatibility. "
                "Update SUPABASE_POOLER_URL to use port 6543."
            )
        print(f"[DATABASE] Connecting to Supabase PostgreSQL...")
        return supabase_url, False

    # Fallback: SQLite for local development
    if config.database.sqlite_fallback_enabled:
        sqlite_path = config.database.sqlite_fallback_path
        os.makedirs(os.path.dirname(os.path.abspath(sqlite_path)), exist_ok=True)
        url = f"sqlite:///{sqlite_path}"
        print(
            f"[DATABASE] SUPABASE_POOLER_URL not set. "
            f"Using SQLite fallback: {sqlite_path}. "
            f"Set SUPABASE_POOLER_URL environment variable for production."
        )
        return url, True

    raise EnvironmentError(
        "No database configured. Set SUPABASE_POOLER_URL environment variable "
        "or enable sqlite_fallback in config.yaml."
    )


# -----------------------------------------------------------------------------
# ENGINE FACTORY
# Singleton engine — created once per process
# -----------------------------------------------------------------------------

_engine: Engine | None = None
_is_sqlite: bool = False


def get_engine() -> Engine:
    """
    Returns the SQLAlchemy engine singleton.
    Creates it on first call — subsequent calls return the same engine.

    Engine configuration:
        pool_pre_ping=True:  Verifies connection before use
                             Handles Supabase connection lifecycle
        pool_recycle=300:    Recycles connections every 5 minutes
                             Prevents NAT timeout disconnections
        pool_size=5:         Limits concurrent connections
                             Respects Supabase free tier limits
        sslmode=require:     Enforces TLS for financial data
    """
    global _engine, _is_sqlite

    if _engine is not None:
        return _engine

    db_url, is_sqlite = _build_database_url()
    _is_sqlite = is_sqlite

    if is_sqlite:
        # SQLite: simpler configuration, no SSL, no pooling
        _engine = create_engine(
            db_url,
            connect_args={"check_same_thread": False},
            echo=False,
        )
        # Enable WAL mode for SQLite — better concurrent read performance
        @event.listens_for(_engine, "connect")
        def set_sqlite_pragma(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    else:
        # PostgreSQL: full production configuration
        _engine = create_engine(
            db_url,
            connect_args={
                "sslmode":          config.database.ssl_mode,
                "application_name": "R2R_Recon_Engine",
                "connect_timeout":  30,
            },
            pool_pre_ping=  config.database.pool_pre_ping,
            pool_recycle=   config.database.pool_recycle,
            pool_size=      config.database.pool_size,
            max_overflow=   config.database.max_overflow,
            echo=           False,
        )

    print(f"[DATABASE] Engine created. Backend: {'SQLite' if is_sqlite else 'PostgreSQL'}")
    return _engine


# -----------------------------------------------------------------------------
# SESSION FACTORY
# Context manager for safe database sessions
# -----------------------------------------------------------------------------

@contextmanager
def get_session() -> Generator[Session, None, None]:
    """
    Context manager that provides a database session.

    Usage:
        with get_session() as session:
            session.add(some_model)
            session.commit()

    On exception: session automatically rolls back.
    Session always closed after the with block.
    """
    engine = get_engine()
    SessionLocal = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
    )
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# -----------------------------------------------------------------------------
# SOX AUDIT TRIGGER
# PostgreSQL kernel-level trigger for immutable audit trail
# Skipped on SQLite (not supported)
# -----------------------------------------------------------------------------

SOX_TRIGGER_SQL = """
-- ==========================================================================
-- SOX AUDIT LEDGER TRIGGER
-- Fires on every INSERT, UPDATE, DELETE on financially significant tables.
-- Writes an immutable record to sox_audit_ledger.
-- SECURITY DEFINER: runs with creator privileges regardless of caller.
-- The application user has INSERT only on sox_audit_ledger —
-- this trigger bypasses that restriction to write audit records.
-- ==========================================================================

-- Step 1: Create the trigger function
CREATE OR REPLACE FUNCTION trigger_sox_audit_log()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO sox_audit_ledger (
        table_name,
        operation,
        old_record,
        new_record,
        executed_by,
        execution_timestamp
    ) VALUES (
        TG_TABLE_NAME,
        TG_OP,
        CASE WHEN TG_OP = 'DELETE' OR TG_OP = 'UPDATE'
             THEN row_to_json(OLD)::TEXT
             ELSE NULL END,
        CASE WHEN TG_OP = 'INSERT' OR TG_OP = 'UPDATE'
             THEN row_to_json(NEW)::TEXT
             ELSE NULL END,
        current_user,
        NOW()
    );
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Step 2: Attach trigger to match_pairs
DROP TRIGGER IF EXISTS sox_audit_match_pairs ON match_pairs;
CREATE TRIGGER sox_audit_match_pairs
    AFTER INSERT OR UPDATE OR DELETE ON match_pairs
    FOR EACH ROW EXECUTE FUNCTION trigger_sox_audit_log();

-- Step 3: Attach trigger to exceptions
DROP TRIGGER IF EXISTS sox_audit_exceptions ON exceptions;
CREATE TRIGGER sox_audit_exceptions
    AFTER INSERT OR UPDATE OR DELETE ON exceptions
    FOR EACH ROW EXECUTE FUNCTION trigger_sox_audit_log();

-- Step 4: Attach trigger to je_drafts
DROP TRIGGER IF EXISTS sox_audit_je_drafts ON je_drafts;
CREATE TRIGGER sox_audit_je_drafts
    AFTER INSERT OR UPDATE OR DELETE ON je_drafts
    FOR EACH ROW EXECUTE FUNCTION trigger_sox_audit_log();
"""


# -----------------------------------------------------------------------------
# PERIOD LOCK TRIGGER
# PostgreSQL trigger that enforces period lock at database kernel level
# Rejects any modification to a locked period — even from Python bugs
# -----------------------------------------------------------------------------

PERIOD_LOCK_TRIGGER_SQL = """
-- ==========================================================================
-- PERIOD LOCK TRIGGER
-- Fires BEFORE any INSERT, UPDATE, DELETE on match_pairs or exceptions.
-- If the period is locked in financial_periods, the operation is rejected.
-- This is enforced at the database kernel level — Python cannot override it.
-- ==========================================================================

CREATE OR REPLACE FUNCTION check_period_lock()
RETURNS TRIGGER AS $$
DECLARE
    is_locked BOOLEAN;
    target_period TEXT;
BEGIN
    -- Determine the period from the row being modified
    target_period := COALESCE(NEW.period, OLD.period);

    -- Look up lock status
    SELECT period_locked INTO is_locked
    FROM financial_periods
    WHERE period = target_period;

    -- If period is locked, reject the operation
    IF is_locked IS TRUE THEN
        RAISE EXCEPTION
            'SOX ITGC VIOLATION: Period % is locked. '
            'Cannot modify financial data for a closed period. '
            'Contact the Head of Accounting to investigate.',
            target_period;
    END IF;

    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

-- Attach to match_pairs
DROP TRIGGER IF EXISTS enforce_period_lock_match_pairs ON match_pairs;
CREATE TRIGGER enforce_period_lock_match_pairs
    BEFORE INSERT OR UPDATE OR DELETE ON match_pairs
    FOR EACH ROW EXECUTE FUNCTION check_period_lock();

-- Attach to exceptions
DROP TRIGGER IF EXISTS enforce_period_lock_exceptions ON exceptions;
CREATE TRIGGER enforce_period_lock_exceptions
    BEFORE INSERT OR UPDATE OR DELETE ON exceptions
    FOR EACH ROW EXECUTE FUNCTION check_period_lock();
"""


# -----------------------------------------------------------------------------
# READ-ONLY ROLE SETUP
# Creates a read-only role for backup controllers and auditors
# They can query the database but cannot modify any data
# -----------------------------------------------------------------------------

READONLY_ROLE_SQL = """
-- ==========================================================================
-- READ-ONLY ROLE FOR BACKUP CONTROLLERS AND AUDITORS
-- Controllers can connect via DBeaver or Excel Power Query
-- and query all tables without risk of accidental modification.
-- ==========================================================================

-- Create the role if it doesn't exist
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'readonly_group') THEN
        CREATE ROLE readonly_group NOLOGIN;
    END IF;
END $$;

-- Grant connect and schema usage
GRANT CONNECT ON DATABASE postgres TO readonly_group;
GRANT USAGE ON SCHEMA public TO readonly_group;

-- Grant SELECT on all existing tables
GRANT SELECT ON ALL TABLES IN SCHEMA public TO readonly_group;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO readonly_group;

-- Prevent accidental table creation
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- Ensure future tables are also readable
-- (Replace 'authenticator' with your Supabase service role name if different)
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO readonly_group;
"""


# -----------------------------------------------------------------------------
# DATABASE INITIALISER
# Creates all tables and sets up triggers
# Called once on first run — idempotent (safe to call multiple times)
# -----------------------------------------------------------------------------

def init_db(create_readonly_role: bool = False) -> None:
    """
    Initialises the database.

    Actions:
    1. Creates all tables defined in models.py (if they don't exist)
    2. Creates SOX audit trigger (PostgreSQL only)
    3. Creates period lock trigger (PostgreSQL only)
    4. Optionally creates read-only role (PostgreSQL only)

    Idempotent: safe to call on every pipeline run.
    Tables that already exist are not recreated.
    Triggers use CREATE OR REPLACE — safe to re-run.

    Args:
        create_readonly_role: Set True on first setup to create
                              the read-only role for controllers.
                              Default False — skip on regular runs.
    """
    engine = get_engine()

    print("[DATABASE] Initialising database...")

    # Create all tables from models.py
    Base.metadata.create_all(engine, checkfirst=True)
    print(f"[DATABASE] Tables confirmed: {[m.__tablename__ for m in ALL_MODELS]}")

    # PostgreSQL-only: triggers and roles
    if not _is_sqlite:
        with engine.connect() as conn:
            # SOX audit trigger
            try:
                conn.execute(text(SOX_TRIGGER_SQL))
                conn.commit()
                print("[DATABASE] SOX audit trigger: installed")
            except Exception as e:
                print(f"[DATABASE] SOX trigger warning: {e}")
                print("[DATABASE] This may be a permissions issue on free tier.")
                print("[DATABASE] Audit trail will rely on application-level logging.")

            # Period lock trigger
            try:
                conn.execute(text(PERIOD_LOCK_TRIGGER_SQL))
                conn.commit()
                print("[DATABASE] Period lock trigger: installed")
            except Exception as e:
                print(f"[DATABASE] Period lock trigger warning: {e}")

            # Read-only role (first-time setup only)
            if create_readonly_role:
                try:
                    conn.execute(text(READONLY_ROLE_SQL))
                    conn.commit()
                    print("[DATABASE] Read-only role: created")
                except Exception as e:
                    print(f"[DATABASE] Read-only role warning: {e}")

    else:
        print(
            "[DATABASE] SQLite mode: PostgreSQL triggers skipped. "
            "SOX audit trail relies on application-level logging only. "
            "Migrate to Supabase for full SOX compliance."
        )

    print("[DATABASE] Initialisation complete.")


# -----------------------------------------------------------------------------
# CONNECTION TEST
# Quick health check — used by preflight_check.py
# -----------------------------------------------------------------------------

def test_connection() -> bool:
    """
    Tests database connectivity.
    Returns True if connection succeeds, False otherwise.
    Used by preflight_check.py before demos.
    """
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        print(f"[DATABASE] Connection test failed: {e}")
        return False


# -----------------------------------------------------------------------------
# STANDALONE TEST
# Run directly to test database connection and initialisation:
#   python src/persistence/database.py
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

    print("Testing database connection and initialisation...")
    print(f"SQLite fallback enabled: {config.database.sqlite_fallback_enabled}")

    # Test connection
    connected = test_connection()
    print(f"Connection test: {'PASS' if connected else 'FAIL'}")

    if connected:
        # Initialise database
        init_db(create_readonly_role=False)

        # Verify tables exist
        engine = get_engine()
        from sqlalchemy import inspect
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        print(f"\nTables in database: {tables}")

        expected = [m.__tablename__ for m in ALL_MODELS]
        missing = [t for t in expected if t not in tables]

        if missing:
            print(f"WARNING: Missing tables: {missing}")
        else:
            print("All expected tables confirmed.")
