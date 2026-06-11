# =============================================================================
# R2R INTERCOMPANY RECONCILIATION ENGINE
# src/persistence/models.py
#
# What this file does:
#   Defines the database schema as Python classes using SQLAlchemy ORM.
#   Every table in Supabase PostgreSQL is defined here.
#   Running database.py will create all these tables automatically.
#
#   Tables defined:
#     gl_lines          Every ingested GL row from NetSuite
#     match_pairs       All confirmed matched pairs (Tiers 1-4)
#     exceptions        All unmatched items with full history
#     je_drafts         Auto-generated correcting journal entries
#     run_log           Immutable execution audit trail
#     financial_periods Period lock table (SOX control)
#     notification_log  Alert delivery tracking
#     resolution_patterns Learned patterns from human resolutions
#     sox_audit_ledger  Immutable append-only SOX audit trail
#                       (also enforced by PostgreSQL trigger in database.py)
#
#   Design principles:
#     - Every table has created_at timestamp
#     - Financial tables have run_id for traceability
#     - No CASCADE deletes — financial history is never automatically removed
#     - Soft deletes only (status column) — nothing is ever hard deleted
#
# How other files use it:
#   from src.persistence.models import Base, GLLine, MatchPair, Exception_
#   (imported by database.py to create tables)
#   (imported by repository.py for all read/write operations)
# =============================================================================

from datetime import datetime
from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer,
    String, Text, UniqueConstraint, Index,
    ForeignKey, Numeric,
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.dialects.postgresql import UUID
import uuid


# -----------------------------------------------------------------------------
# BASE
# All models inherit from this — SQLAlchemy requirement
# -----------------------------------------------------------------------------

Base = declarative_base()


# -----------------------------------------------------------------------------
# HELPER
# Reusable default for UUID primary keys
# -----------------------------------------------------------------------------

def _uuid():
    return str(uuid.uuid4())


# -----------------------------------------------------------------------------
# TABLE 1: GL LINES
# Every raw GL row ingested from a NetSuite CSV export.
# Idempotent: same internalid + linesequencenumber + file_md5 = no duplicate.
# -----------------------------------------------------------------------------

class GLLine(Base):
    """
    Stores every GL line from the NetSuite export.
    One row per line item in the CSV.

    Idempotency constraint:
        UNIQUE(internalid, linesequencenumber, input_file_md5)
        Re-ingesting the same file produces no new rows.
    """
    __tablename__ = "gl_lines"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    run_id              = Column(String(36),  nullable=False, index=True)
    input_file_md5      = Column(String(32),  nullable=False)

    # NetSuite fields — exact column names from config.yaml
    internalid          = Column(String(50),  nullable=False)
    linesequencenumber  = Column(Integer,      nullable=False)
    trandate            = Column(String(20),   nullable=False)
    tranid              = Column(String(100),  nullable=True)
    account_raw         = Column(String(500),  nullable=False)  # Raw concatenated string
    account_code        = Column(String(20),   nullable=False)  # Parsed: "18500"
    account_name        = Column(String(255),  nullable=False)  # Parsed: "IC Receivable"
    debitamount         = Column(Numeric(18,2), nullable=True)
    creditamount        = Column(Numeric(18,2), nullable=True)
    net_amount          = Column(Numeric(18,2), nullable=False)  # debit - credit
    currency            = Column(String(3),    nullable=False)
    fxamount            = Column(Numeric(18,2), nullable=True)
    line_subsidiary     = Column(String(100),  nullable=False)

    # Classification fields (added by classifier.py)
    local_entity        = Column(String(20),   nullable=False)
    cseg_apac_ic        = Column(String(20),   nullable=True)
    cseg_normalised     = Column(String(20),   nullable=True)  # After fuzzy correction
    ic_source           = Column(String(20),   nullable=True)  # CSEG|ACCOUNT_CODE|MEMO
    ic_counterparty     = Column(String(20),   nullable=True)
    memo                = Column(Text,          nullable=True)

    # Key fields (added by keygen.py)
    row_id              = Column(String(64),   nullable=True, unique=True)
    tranid_key          = Column(String(200),  nullable=True)
    match_group_key     = Column(String(64),   nullable=True, index=True)

    # Flags
    is_ic               = Column(Boolean, default=False)
    is_fx_coalesced     = Column(Boolean, default=False)
    is_cseg_corrected   = Column(Boolean, default=False)
    is_partial_reversal = Column(Boolean, default=False)
    is_out_of_scope     = Column(Boolean, default=False)
    is_reversal         = Column(Boolean, default=False)

    ingested_at         = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "internalid", "linesequencenumber", "input_file_md5",
            name="uq_gl_line_idempotency"
        ),
        Index("ix_gl_lines_entity_period",
              "local_entity", "trandate"),
        Index("ix_gl_lines_match_key",
              "match_group_key"),
    )

    def __repr__(self):
        return (
            f"<GLLine {self.internalid}-{self.linesequencenumber} "
            f"{self.local_entity} {self.fxamount} {self.currency}>"
        )


# -----------------------------------------------------------------------------
# TABLE 2: MATCH PAIRS
# All confirmed matched pairs from Tiers 1-4.
# One row per matched pair (not per GL line).
# -----------------------------------------------------------------------------

class MatchPair(Base):
    """
    Stores every confirmed IC match.
    Each row represents one matched pair — two GL lines resolved.

    SOX boolean columns (is_entity_matched etc.) allow auditors to
    verify matching criteria without reading the matching code.
    """
    __tablename__ = "match_pairs"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    match_id        = Column(String(36), nullable=False,
                             default=_uuid, unique=True)
    run_id          = Column(String(36), nullable=False, index=True)
    period          = Column(String(20), nullable=False, index=True)

    # Originator side
    orig_internalid  = Column(String(50),  nullable=False)
    orig_entity      = Column(String(20),  nullable=False)
    orig_trandate    = Column(String(20),  nullable=False)
    orig_currency    = Column(String(3),   nullable=False)
    orig_fxamount    = Column(Numeric(18,2), nullable=False)
    orig_account_code = Column(String(20), nullable=True)
    orig_account_name = Column(String(255), nullable=True)
    orig_row_id      = Column(String(64),  nullable=True)
    orig_ic_source   = Column(String(20),  nullable=True)

    # Receiver side
    recv_internalid  = Column(String(50),  nullable=False)
    recv_entity      = Column(String(20),  nullable=False)
    recv_trandate    = Column(String(20),  nullable=False)
    recv_currency    = Column(String(3),   nullable=False)
    recv_fxamount    = Column(Numeric(18,2), nullable=False)
    recv_account_code = Column(String(20), nullable=True)
    recv_account_name = Column(String(255), nullable=True)
    recv_row_id      = Column(String(64),  nullable=True)
    recv_ic_source   = Column(String(20),  nullable=True)

    # Match metadata
    match_tier           = Column(String(30),  nullable=False)
    confidence_score     = Column(Integer,      nullable=False)
    group_pairing_id     = Column(String(36),  nullable=True)  # Tier 4 subset sum

    # SOX boolean decomposition — auditor-verifiable without code
    is_entity_matched    = Column(Boolean, nullable=False)
    is_currency_matched  = Column(Boolean, nullable=False)
    is_fxamount_exact    = Column(Boolean, nullable=False)
    is_period_matched    = Column(Boolean, nullable=False)

    # Variance decomposition
    variance_fxamount    = Column(Numeric(18,2), nullable=True)
    variance_usd         = Column(Numeric(18,2), nullable=True)
    fx_rounding_variance = Column(Numeric(18,2), nullable=True)
    operational_variance = Column(Numeric(18,2), nullable=True)
    tolerance_reason     = Column(String(30),  nullable=True)
    fuzzy_account_score  = Column(Float,        nullable=True)
    period_delta_months  = Column(Integer,      nullable=True)

    matched_at           = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_match_pairs_period_entity",
              "period", "orig_entity", "recv_entity"),
    )

    def __repr__(self):
        return (
            f"<MatchPair {self.orig_entity}↔{self.recv_entity} "
            f"{self.orig_fxamount} {self.orig_currency} "
            f"Tier:{self.match_tier} Score:{self.confidence_score}>"
        )


# -----------------------------------------------------------------------------
# TABLE 3: EXCEPTIONS
# All unmatched items with full history across runs.
# NEW/RECURRING/RESOLVED status tracked automatically.
# -----------------------------------------------------------------------------

class Exception_(Base):
    """
    Stores every unmatched IC transaction.
    Status tracks lifecycle: OPEN → RESOLVED | ESCALATED | ACCEPTED.

    Period-over-period memory:
        first_seen_run_id:  When this exception first appeared
        last_seen_run_id:   Most recent run that saw it
        occurrence_count:   How many runs it has appeared in
        resolved_run_id:    Run where it was finally matched
    """
    __tablename__ = "exceptions"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    exception_id    = Column(String(36), nullable=False,
                             default=_uuid, unique=True)
    run_id          = Column(String(36), nullable=False, index=True)
    period          = Column(String(20), nullable=False, index=True)

    # Source transaction details
    gl_row_id       = Column(String(64),  nullable=True)
    internalid      = Column(String(50),  nullable=True)
    source_entity   = Column(String(20),  nullable=False)
    target_cseg     = Column(String(20),  nullable=True)
    trandate        = Column(String(20),  nullable=False)
    unmatched_fxamount = Column(Numeric(18,2), nullable=True)
    unmatched_currency = Column(String(3),     nullable=True)
    account_code    = Column(String(20),  nullable=True)
    account_name    = Column(String(255), nullable=True)
    memo            = Column(Text,         nullable=True)
    ic_source       = Column(String(20),  nullable=True)

    # Exception classification
    exception_type  = Column(String(30),  nullable=False, index=True)
    failure_reason  = Column(Text,         nullable=True)
    priority        = Column(String(5),   nullable=False, index=True)

    # Aging
    days_aged       = Column(Integer,     nullable=False, default=0)
    aging_bucket    = Column(String(10),  nullable=False, default="0-30")

    # Period-over-period memory
    first_seen_run_id  = Column(String(36), nullable=False)
    last_seen_run_id   = Column(String(36), nullable=False)
    occurrence_count   = Column(Integer,    nullable=False, default=1)
    period_status      = Column(String(20), nullable=False,
                                default="NEW")   # NEW|RECURRING|RESOLVED

    # Lifecycle
    status          = Column(String(20),  nullable=False,
                             default="OPEN", index=True)
    resolved_run_id = Column(String(36),  nullable=True)
    resolved_at     = Column(DateTime,    nullable=True)

    # LLM suggestion (Tier 5)
    llm_suggested_entity = Column(String(20),  nullable=True)
    llm_confidence       = Column(Float,        nullable=True)
    llm_reasoning        = Column(String(500),  nullable=True)
    llm_routing          = Column(String(30),   nullable=True)

    # Human-in-the-loop (controller actions)
    controller_note   = Column(Text,        nullable=True)
    controller_action = Column(String(30),  nullable=True)
    actioned_by       = Column(String(200), nullable=True)
    actioned_at       = Column(DateTime,    nullable=True)

    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow,
                             onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_exceptions_entity_period",
              "source_entity", "period"),
        Index("ix_exceptions_status_priority",
              "status", "priority"),
    )

    def __repr__(self):
        return (
            f"<Exception {self.exception_type} "
            f"{self.source_entity} {self.unmatched_fxamount} "
            f"{self.priority} {self.status}>"
        )


# -----------------------------------------------------------------------------
# TABLE 4: JE DRAFTS
# Auto-generated correcting journal entries.
# Human approves — never auto-posted to NetSuite.
# -----------------------------------------------------------------------------

class JEDraft(Base):
    """
    Stores auto-generated journal entry drafts.

    These are generated for:
        - Tier 4 tolerance matches (FX rounding adjustments)
        - Timing gap in-transit accruals
        - Tier 5 orphan clearance suggestions

    Human workflow:
        1. Controller reviews Tab 3 in Excel
        2. Controller ticks approved_by column
        3. Controller manually posts to NetSuite
        4. Controller updates posted_to_netsuite = True
        5. Next pipeline run checks for outstanding unposted JEs
    """
    __tablename__ = "je_drafts"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    je_draft_id     = Column(String(50),  nullable=False, unique=True)
    run_id          = Column(String(36),  nullable=False, index=True)
    period          = Column(String(20),  nullable=False)

    # Source linkage — traces back to Tab 1 or Tab 2
    source_match_id     = Column(String(36), nullable=True)
    source_exception_id = Column(String(36), nullable=True)

    # JE classification
    je_type         = Column(String(30),  nullable=False)
    # FX_ROUNDING | IN_TRANSIT | TOLERANCE_ADJ | ORPHAN_CLEARANCE

    generated_by    = Column(String(30),  nullable=False)
    # TIER3_FX | TIER3_TRANSIT | TIER4_SUBSET | TIER5_ORPHAN | LLM_SUGGESTED

    # JE details
    subsidiary      = Column(String(20),  nullable=False)
    account_code    = Column(String(20),  nullable=False)
    debit           = Column(Numeric(18,2), nullable=True)
    credit          = Column(Numeric(18,2), nullable=True)
    currency        = Column(String(3),   nullable=False)
    fxamount        = Column(Numeric(18,2), nullable=True)
    memo            = Column(Text,         nullable=False)

    # Human-in-the-loop controls
    requires_review     = Column(Boolean, nullable=False, default=True)
    approved_by         = Column(String(200), nullable=True)
    approved_at         = Column(DateTime,    nullable=True)
    posted_to_netsuite  = Column(Boolean, nullable=False, default=False)
    posted_at           = Column(DateTime,    nullable=True)

    # SLA tracking for unposted JE alerts
    created_at      = Column(DateTime, default=datetime.utcnow)
    last_alerted_at = Column(DateTime, nullable=True)
    alert_count     = Column(Integer,  nullable=False, default=0)

    def __repr__(self):
        return (
            f"<JEDraft {self.je_draft_id} {self.je_type} "
            f"{self.subsidiary} {self.fxamount} {self.currency} "
            f"Posted:{self.posted_to_netsuite}>"
        )


# -----------------------------------------------------------------------------
# TABLE 5: RUN LOG
# Immutable execution audit trail.
# One row per pipeline run. Never updated — append only.
# -----------------------------------------------------------------------------

class RunLog(Base):
    """
    Immutable log of every pipeline execution.

    Written at the start of each run (status=RUNNING) and
    updated at completion (status=COMPLETE|FAILED).

    The MD5 hash proves which exact file was processed.
    The config_version proves which exact config was used.
    Together they satisfy SOX chain of custody requirements.
    """
    __tablename__ = "run_log"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    run_id          = Column(String(36),  nullable=False, unique=True)
    period          = Column(String(20),  nullable=False)
    input_file      = Column(String(500), nullable=False)
    input_md5       = Column(String(32),  nullable=False)
    config_version  = Column(String(64),  nullable=True)

    # Counts
    records_in          = Column(Integer, nullable=True)
    ic_identified       = Column(Integer, nullable=True)
    reversals_dropped   = Column(Integer, nullable=True)
    out_of_scope        = Column(Integer, nullable=True)
    tier1_matches       = Column(Integer, nullable=True)
    tier2_matches       = Column(Integer, nullable=True)
    tier3_matches       = Column(Integer, nullable=True)
    tier4_matches       = Column(Integer, nullable=True)
    llm_suggestions     = Column(Integer, nullable=True)
    exceptions_open     = Column(Integer, nullable=True)
    llm_calls_made      = Column(Integer, nullable=True)

    # STP metrics
    stp_rate            = Column(Float,   nullable=True)
    stp_previous_run    = Column(Float,   nullable=True)
    stp_delta           = Column(Float,   nullable=True)
    stp_alert_fired     = Column(Boolean, nullable=True)

    # Execution
    runtime_seconds     = Column(Float,   nullable=True)
    status              = Column(String(20), nullable=False,
                                 default="RUNNING")
    error_message       = Column(Text,    nullable=True)
    started_at          = Column(DateTime, nullable=False,
                                 default=datetime.utcnow)
    completed_at        = Column(DateTime, nullable=True)

    def __repr__(self):
        return (
            f"<RunLog {self.run_id[:8]}... "
            f"{self.period} {self.status} "
            f"STP:{self.stp_rate:.1%}>" if self.stp_rate else
            f"<RunLog {self.run_id[:8]}... {self.period} {self.status}>"
        )


# -----------------------------------------------------------------------------
# TABLE 6: FINANCIAL PERIODS
# Period lock table — SOX control.
# Once a period is locked, PostgreSQL trigger rejects all modifications.
# -----------------------------------------------------------------------------

class FinancialPeriod(Base):
    """
    Tracks the lock status of each financial period.

    Once period_locked = True:
        PostgreSQL trigger (created in database.py) rejects any
        INSERT/UPDATE/DELETE on match_pairs or exceptions for that period.
        This is enforced at the database kernel level — Python code
        cannot override it even with a bug.

    Sign-off workflow:
        Controller triggers period_lock.yml GitHub Actions workflow.
        Workflow requires manual approval (GitHub environment protection).
        On approval, executes: UPDATE financial_periods SET period_locked=TRUE.
        This generates an immutable audit log entry.
    """
    __tablename__ = "financial_periods"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    period          = Column(String(20),  nullable=False, unique=True)
    period_locked   = Column(Boolean,     nullable=False, default=False)
    locked_by       = Column(String(200), nullable=True)
    locked_at       = Column(DateTime,    nullable=True)
    lock_run_id     = Column(String(36),  nullable=True)
    created_at      = Column(DateTime,    default=datetime.utcnow)

    def __repr__(self):
        lock_str = f"LOCKED by {self.locked_by}" if self.period_locked else "OPEN"
        return f"<FinancialPeriod {self.period} {lock_str}>"


# -----------------------------------------------------------------------------
# TABLE 7: NOTIFICATION LOG
# Tracks every alert email/Telegram message sent.
# Detects bounces and stale escalation contacts.
# -----------------------------------------------------------------------------

class NotificationLog(Base):
    """
    Immutable log of every alert sent by the notification engine.

    Delivery tracking allows the system to:
        - Detect if escalation emails bounced
        - Know when to send follow-up reminders
        - Prove to auditors that counterparties were notified
    """
    __tablename__ = "notification_log"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    run_id          = Column(String(36),  nullable=False, index=True)
    exception_id    = Column(String(36),  nullable=True)
    je_draft_id     = Column(String(50),  nullable=True)

    notification_type = Column(String(30), nullable=False)
    # EXCEPTION_ALERT | ESCALATION_TIER1 | ESCALATION_TIER2 |
    # ESCALATION_TIER3 | JE_REMINDER | CONFIG_STALENESS | STP_ALERT

    channel         = Column(String(20),  nullable=False)  # EMAIL | TELEGRAM
    recipient       = Column(String(500), nullable=False)
    subject         = Column(String(500), nullable=True)
    sent_at         = Column(DateTime,    default=datetime.utcnow)
    delivery_status = Column(String(20),  nullable=False, default="SENT")
    # SENT | DELIVERED | BOUNCED | FAILED

    def __repr__(self):
        return (
            f"<NotificationLog {self.notification_type} "
            f"to {self.recipient} {self.delivery_status}>"
        )


# -----------------------------------------------------------------------------
# TABLE 8: RESOLUTION PATTERNS
# Learned patterns from human resolutions.
# After 3 identical resolutions, the system auto-resolves future occurrences.
# -----------------------------------------------------------------------------

class ResolutionPattern(Base):
    """
    Stores patterns learned from human exception resolutions.

    When a controller resolves an exception in a specific way
    (e.g., accepts an ACCOUNT_MISMATCH between accounts 61200 and 61250
    for ANTH-SG↔ANTH-JP), the pattern is stored here.

    After times_seen >= 3 (configurable), the engine auto-resolves
    future occurrences of the same pattern and flags them as
    LEARNED_PATTERN in the exceptions tab.

    This mimics ML-based auto-learning without requiring model training.
    """
    __tablename__ = "resolution_patterns"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    pattern_id          = Column(String(36), nullable=False,
                                 default=_uuid, unique=True)

    # Pattern signature
    entity_pair         = Column(String(50),  nullable=False)
    account_code_a      = Column(String(20),  nullable=True)
    account_code_b      = Column(String(20),  nullable=True)
    exception_type      = Column(String(30),  nullable=False)
    resolution_type     = Column(String(30),  nullable=False)

    # Tolerance captured from the resolution
    amount_tolerance_pct = Column(Float,      nullable=True)
    timing_gap_days      = Column(Integer,    nullable=True)

    # Learning metrics
    times_seen          = Column(Integer, nullable=False, default=1)
    auto_resolve_enabled = Column(Boolean, nullable=False, default=False)
    # True when times_seen >= 3

    # Attribution
    first_resolved_by   = Column(String(200), nullable=True)
    last_resolved_by    = Column(String(200), nullable=True)
    first_seen_at       = Column(DateTime,    default=datetime.utcnow)
    last_seen_at        = Column(DateTime,    default=datetime.utcnow,
                                 onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "entity_pair", "account_code_a", "account_code_b",
            "exception_type", "resolution_type",
            name="uq_resolution_pattern"
        ),
    )

    def __repr__(self):
        return (
            f"<ResolutionPattern {self.entity_pair} "
            f"{self.exception_type} → {self.resolution_type} "
            f"(seen {self.times_seen}x)>"
        )


# -----------------------------------------------------------------------------
# TABLE 9: SOX AUDIT LEDGER
# Immutable append-only cryptographic audit trail.
# Also enforced by PostgreSQL SECURITY DEFINER trigger in database.py.
# UPDATE, DELETE, TRUNCATE are revoked from the application user.
# -----------------------------------------------------------------------------

class SOXAuditLedger(Base):
    """
    Immutable, append-only audit ledger for SOX ITGC compliance.

    Every modification to financially significant tables is captured here
    by a PostgreSQL SECURITY DEFINER trigger — not by application code.

    This means:
        Even if there is a bug in Python that tries to delete a record,
        the trigger fires BEFORE the deletion and logs it.
        Even if the pipeline crashes mid-execution, partial changes
        are captured in the ledger before the rollback.

    The application user has INSERT only — no UPDATE, DELETE, TRUNCATE.
    Only the trigger function (running as SECURITY DEFINER) can write here.

    Auditors can query this table to see exactly:
        - What changed
        - What it was before
        - What it became after
        - Who (which database role) made the change
        - Exactly when it happened
    """
    __tablename__ = "sox_audit_ledger"

    audit_id        = Column(String(36), primary_key=True, default=_uuid)
    table_name      = Column(String(100), nullable=False)
    operation       = Column(String(10),  nullable=False)  # INSERT|UPDATE|DELETE
    old_record      = Column(Text,        nullable=True)   # JSON of old values
    new_record      = Column(Text,        nullable=True)   # JSON of new values
    executed_by     = Column(String(200), nullable=False)  # Database role
    execution_timestamp = Column(DateTime, nullable=False,
                                  default=datetime.utcnow)

    __table_args__ = (
        Index("ix_sox_ledger_table_timestamp",
              "table_name", "execution_timestamp"),
    )

    def __repr__(self):
        return (
            f"<SOXAuditLedger {self.operation} on {self.table_name} "
            f"by {self.executed_by} at {self.execution_timestamp}>"
        )


# -----------------------------------------------------------------------------
# ALL MODELS — exported for use by database.py
# -----------------------------------------------------------------------------

ALL_MODELS = [
    GLLine,
    MatchPair,
    Exception_,
    JEDraft,
    RunLog,
    FinancialPeriod,
    NotificationLog,
    ResolutionPattern,
    SOXAuditLedger,
]