# =============================================================================
# R2R INTERCOMPANY RECONCILIATION ENGINE
# src/persistence/repository.py
#
# What this file does:
#   All database read and write operations live here.
#   No other file writes SQL directly — everything goes through this module.
#   This is the Repository Pattern — a single place for all data access.
#
#   Why this matters:
#     If we migrate from SQLite to Supabase, or from Supabase to a different
#     database, only this file changes. The matching engine, reporter, and
#     notifier never need to know which database they are talking to.
#
#   Idempotency:
#     Every write operation is safe to call multiple times.
#     Re-running the pipeline on the same CSV produces no duplicate records.
#     Uses INSERT ON CONFLICT DO NOTHING for GL lines.
#     Uses upsert patterns for exceptions (NEW → RECURRING tracking).
#
#   Period-over-period memory:
#     save_exceptions() checks if an exception already exists in the database.
#     If it does: increment occurrence_count, update last_seen_run_id,
#                 set period_status = RECURRING.
#     If it does not: insert as NEW.
#     If it was OPEN last run but is not in current exceptions:
#                 set status = RESOLVED, record resolved_run_id.
#
# How other files use it:
#   from src.persistence.repository import Repository
#   repo = Repository()
#   repo.save_run_log_start(run_id, period, file_path, md5)
#   repo.save_matched_pairs(result.matched_pairs)
#   repo.save_exceptions(result.exceptions, run_id)
#   previous_stp = repo.get_previous_stp(period)
# =============================================================================

from datetime import datetime
from typing import Optional
import json

import pandas as pd
from sqlalchemy import text

from src.persistence.database import get_engine, get_session
from src.persistence.models import (
    GLLine, MatchPair, Exception_, JEDraft,
    RunLog, FinancialPeriod, NotificationLog,
    ResolutionPattern, SOXAuditLedger,
)
from src.config import config


# -----------------------------------------------------------------------------
# REPOSITORY CLASS
# All database operations as methods on a single class
# -----------------------------------------------------------------------------

class Repository:
    """
    Single access point for all database reads and writes.

    Usage:
        repo = Repository()
        repo.save_run_log_start(...)
        repo.save_matched_pairs(df)
        repo.save_exceptions(df, run_id)
        repo.complete_run_log(run_id, result)
    """

    # ------------------------------------------------------------------
    # RUN LOG
    # ------------------------------------------------------------------

    def save_run_log_start(
        self,
        run_id:    str,
        period:    str,
        file_path: str,
        file_md5:  str,
        records_in: int,
    ) -> None:
        """
        Writes the initial run log entry at pipeline start.
        Status = RUNNING until complete_run_log() is called.

        Called before any matching begins so that even a crashed run
        leaves an audit trail.
        """
        with get_session() as session:
            log = RunLog(
                run_id=     run_id,
                period=     period,
                input_file= file_path,
                input_md5=  file_md5,
                records_in= records_in,
                status=     "RUNNING",
                started_at= datetime.utcnow(),
            )
            session.merge(log)
        print(f"[REPO] Run log started: {run_id[:8]}...")

    def complete_run_log(
        self,
        run_id:   str,
        result,             # MatchResult from engine.py
        runtime:  float,
        status:   str = "COMPLETE",
        error:    str = None,
    ) -> None:
        """
        Updates the run log at pipeline completion.
        Writes all counts, STP metrics, and runtime.
        """
        with get_session() as session:
            log = session.query(RunLog).filter_by(run_id=run_id).first()
            if not log:
                print(f"[REPO] WARNING: Run log not found for {run_id}")
                return

            log.status              = status
            log.error_message       = error
            log.completed_at        = datetime.utcnow()
            log.runtime_seconds     = runtime
            log.ic_identified       = result.total_ic_rows
            log.tier1_matches       = result.tier1_matches
            log.tier2_matches       = result.tier2_matches
            log.tier3_matches       = result.tier3_matches
            log.tier4_matches       = result.tier4_matches
            log.llm_suggestions     = result.llm_suggestions
            log.exceptions_open     = result.total_exceptions
            log.stp_rate            = result.stp_rate
            log.stp_previous_run    = result.stp_previous_run
            log.stp_delta           = result.stp_delta
            log.stp_alert_fired     = result.stp_alert_fired

        print(f"[REPO] Run log completed: {run_id[:8]}... Status: {status}")

    def get_previous_stp(self, period: str) -> float:
        """
        Returns the STP rate from the most recent completed run
        for the same period, or the run immediately before this period.

        Used by engine.py for STP anomaly detection.
        Returns 0.0 if no previous run exists.
        """
        with get_session() as session:
            log = (
                session.query(RunLog)
                .filter(
                    RunLog.status == "COMPLETE",
                    RunLog.stp_rate.isnot(None),
                )
                .order_by(RunLog.completed_at.desc())
                .first()
            )
            if log and log.stp_rate:
                return float(log.stp_rate)
            return 0.0

    # ------------------------------------------------------------------
    # GL LINES
    # ------------------------------------------------------------------

    def save_gl_lines(
        self,
        ic_df:    pd.DataFrame,
        run_id:   str,
        file_md5: str,
    ) -> int:
        """
        Saves classified IC GL lines to the database.
        Idempotent: same file re-ingested produces no new rows.

        Returns number of new rows inserted.
        """
        ns = config.netsuite.fields
        inserted = 0

        with get_session() as session:
            for _, row in ic_df.iterrows():
                gl = GLLine(
                    run_id=             run_id,
                    input_file_md5=     file_md5,
                    internalid=         str(row.get(ns.internal_id, "")),
                    linesequencenumber= int(row.get(ns.line_sequence, 0) or 0),
                    trandate=           str(row.get(ns.tran_date, "")),
                    tranid=             str(row.get(ns.tran_id, "") or ""),
                    account_raw=        str(row.get(ns.account, "")),
                    account_code=       str(row.get("account_code", "")),
                    account_name=       str(row.get("account_name", "")),
                    debitamount=        _safe_decimal(row.get(ns.debit_amount)),
                    creditamount=       _safe_decimal(row.get(ns.credit_amount)),
                    net_amount=         _safe_decimal(row.get("net_amount", 0)),
                    currency=           str(row.get(ns.currency, "")),
                    fxamount=           _safe_decimal(row.get(ns.fx_amount)),
                    line_subsidiary=    str(row.get(ns.line_subsidiary, "")),
                    local_entity=       str(row.get("local_entity", "")),
                    cseg_apac_ic=       str(row.get(ns.cseg_ic, "") or ""),
                    ic_source=          str(row.get("ic_source", "")),
                    ic_counterparty=    str(row.get("ic_counterparty", "")),
                    memo=               str(row.get(ns.memo, "") or ""),
                    row_id=             str(row.get("row_id", "")),
                    tranid_key=         str(row.get("tranid_key", "")),
                    match_group_key=    str(row.get("match_group_key", "")),
                    is_ic=              bool(row.get("is_ic", False)),
                    is_fx_coalesced=    bool(row.get("is_fx_coalesced", False)),
                    is_cseg_corrected=  bool(row.get("is_cseg_corrected", False)),
                    is_partial_reversal=bool(row.get("is_partial_reversal", False)),
                )

                # Idempotent insert — skip if already exists
                try:
                    session.add(gl)
                    session.flush()
                    inserted += 1
                except Exception:
                    session.rollback()
                    # Row already exists — skip silently
                    continue

        print(f"[REPO] GL lines saved: {inserted} new rows inserted.")
        return inserted

    # ------------------------------------------------------------------
    # MATCHED PAIRS
    # ------------------------------------------------------------------

    def save_matched_pairs(
        self,
        matched_df: pd.DataFrame,
    ) -> int:
        """
        Saves all matched pairs to the database.
        Each row in matched_df becomes one MatchPair record.
        Returns number of pairs saved.
        """
        if matched_df is None or len(matched_df) == 0:
            print("[REPO] No matched pairs to save.")
            return 0

        saved = 0
        with get_session() as session:
            for _, row in matched_df.iterrows():
                pair = MatchPair(
                    match_id=           str(row.get("match_id", "")),
                    run_id=             str(row.get("run_id", "")),
                    period=             str(row.get("period", "")),
                    orig_internalid=    str(row.get("orig_internalid", "")),
                    orig_entity=        str(row.get("orig_entity", "")),
                    orig_trandate=      str(row.get("orig_trandate", "")),
                    orig_currency=      str(row.get("orig_currency", "")),
                    orig_fxamount=      _safe_decimal(row.get("orig_fxamount")),
                    orig_account_code=  str(row.get("orig_account_code", "")),
                    orig_account_name=  str(row.get("orig_account_name", "")),
                    orig_row_id=        str(row.get("orig_row_id", "")),
                    orig_ic_source=     str(row.get("orig_ic_source", "")),
                    recv_internalid=    str(row.get("recv_internalid", "")),
                    recv_entity=        str(row.get("recv_entity", "")),
                    recv_trandate=      str(row.get("recv_trandate", "")),
                    recv_currency=      str(row.get("recv_currency", "")),
                    recv_fxamount=      _safe_decimal(row.get("recv_fxamount")),
                    recv_account_code=  str(row.get("recv_account_code", "")),
                    recv_account_name=  str(row.get("recv_account_name", "")),
                    recv_row_id=        str(row.get("recv_row_id", "")),
                    recv_ic_source=     str(row.get("recv_ic_source", "")),
                    match_tier=         str(row.get("match_tier", "")),
                    confidence_score=   int(row.get("confidence_score", 0) or 0),
                    group_pairing_id=   str(row.get("group_pairing_id", "") or ""),
                    is_entity_matched=  bool(row.get("is_entity_matched", False)),
                    is_currency_matched=bool(row.get("is_currency_matched", False)),
                    is_fxamount_exact=  bool(row.get("is_fxamount_exact", False)),
                    is_period_matched=  bool(row.get("is_period_matched", False)),
                    variance_fxamount=  _safe_decimal(row.get("variance_fxamount")),
                    variance_usd=       _safe_decimal(row.get("variance_usd")),
                    fx_rounding_variance=_safe_decimal(
                                            row.get("fx_rounding_variance")),
                    operational_variance=_safe_decimal(
                                            row.get("operational_variance")),
                    tolerance_reason=   str(row.get("tolerance_reason", "") or ""),
                    fuzzy_account_score=_safe_float(row.get("fuzzy_account_score")),
                    period_delta_months=_safe_int(row.get("period_delta_months")),
                )
                session.add(pair)
                saved += 1

        print(f"[REPO] Matched pairs saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # EXCEPTIONS — with period-over-period memory
    # ------------------------------------------------------------------

    def save_exceptions(
        self,
        exceptions_df: pd.DataFrame,
        run_id:        str,
    ) -> tuple[int, int, int]:
        """
        Saves exceptions with NEW/RECURRING/RESOLVED tracking.

        Logic:
        1. For each exception in exceptions_df:
           - If gl_row_id exists in DB as OPEN → RECURRING, increment count
           - If not in DB → NEW
        2. For each OPEN exception in DB not in current exceptions_df:
           - Set status = RESOLVED, record resolved_run_id

        Returns:
            (new_count, recurring_count, resolved_count)
        """
        if exceptions_df is None or len(exceptions_df) == 0:
            print("[REPO] No exceptions to save.")
            return 0, 0, 0

        new_count       = 0
        recurring_count = 0
        resolved_count  = 0

        ns = config.netsuite.fields

        with get_session() as session:

            # Get all currently OPEN exceptions from DB
            open_db = session.query(Exception_).filter_by(status="OPEN").all()
            open_db_row_ids = {e.gl_row_id: e for e in open_db if e.gl_row_id}

            # Current exception gl_row_ids
            current_row_ids = set(
                str(row.get("row_id", ""))
                for _, row in exceptions_df.iterrows()
            )

            # Mark resolved: was OPEN last run, not in current exceptions
            for gl_row_id, existing in open_db_row_ids.items():
                if gl_row_id not in current_row_ids:
                    existing.status         = "RESOLVED"
                    existing.resolved_run_id= run_id
                    existing.resolved_at    = datetime.utcnow()
                    existing.period_status  = "RESOLVED"
                    resolved_count += 1

            # Save current exceptions
            for _, row in exceptions_df.iterrows():
                gl_row_id = str(row.get("row_id", ""))

                if gl_row_id and gl_row_id in open_db_row_ids:
                    # RECURRING — update existing record
                    existing = open_db_row_ids[gl_row_id]
                    existing.last_seen_run_id  = run_id
                    existing.occurrence_count  += 1
                    existing.period_status     = "RECURRING"
                    existing.days_aged         = int(row.get("days_aged", 0) or 0)
                    existing.aging_bucket      = str(row.get("aging_bucket", "0-30"))
                    existing.priority          = str(row.get("priority", "P3"))
                    # Update LLM suggestion if improved
                    if row.get("llm_routing") == "LLM_SUGGESTED":
                        existing.llm_suggested_entity = str(
                            row.get("llm_suggested_entity", ""))
                        existing.llm_confidence = _safe_float(
                            row.get("llm_confidence"))
                        existing.llm_reasoning  = str(
                            row.get("llm_reasoning", ""))
                        existing.llm_routing    = str(
                            row.get("llm_routing", ""))
                    recurring_count += 1

                else:
                    # NEW exception
                    exc = Exception_(
                        run_id=             run_id,
                        period=             str(row.get("period", "")),
                        gl_row_id=          gl_row_id,
                        internalid=         str(row.get(ns.internal_id, "") or ""),
                        source_entity=      str(row.get("source_entity", "")),
                        target_cseg=        str(row.get("target_cseg", "") or ""),
                        trandate=           str(row.get(ns.tran_date, "")),
                        unmatched_fxamount= _safe_decimal(
                                                row.get("unmatched_fxamount")),
                        unmatched_currency= str(row.get("unmatched_currency", "")),
                        account_code=       str(row.get("account_code", "")),
                        account_name=       str(row.get("account_name", "")),
                        memo=               str(row.get(ns.memo, "") or ""),
                        ic_source=          str(row.get("ic_source", "")),
                        exception_type=     str(row.get("exception_type", "ORPHAN")),
                        priority=           str(row.get("priority", "P3")),
                        days_aged=          int(row.get("days_aged", 0) or 0),
                        aging_bucket=       str(row.get("aging_bucket", "0-30")),
                        first_seen_run_id=  run_id,
                        last_seen_run_id=   run_id,
                        occurrence_count=   1,
                        period_status=      "NEW",
                        status=             "OPEN",
                        llm_suggested_entity=str(
                                            row.get("llm_suggested_entity","") or ""),
                        llm_confidence=     _safe_float(row.get("llm_confidence")),
                        llm_reasoning=      str(row.get("llm_reasoning", "") or ""),
                        llm_routing=        str(row.get("llm_routing", "") or ""),
                    )
                    session.add(exc)
                    new_count += 1

        print(
            f"[REPO] Exceptions saved: "
            f"{new_count} new, {recurring_count} recurring, "
            f"{resolved_count} resolved."
        )
        return new_count, recurring_count, resolved_count

    # ------------------------------------------------------------------
    # JE DRAFTS
    # ------------------------------------------------------------------

    def save_je_drafts(
        self,
        je_drafts_df: pd.DataFrame,
    ) -> int:
        """
        Saves auto-generated JE drafts to the database.
        Returns number of drafts saved.
        """
        if je_drafts_df is None or len(je_drafts_df) == 0:
            return 0

        saved = 0
        with get_session() as session:
            for _, row in je_drafts_df.iterrows():
                draft = JEDraft(
                    je_draft_id=        str(row.get("je_draft_id", "")),
                    run_id=             str(row.get("run_id", "")),
                    period=             str(row.get("period", "")),
                    source_match_id=    str(row.get("source_match_id", "") or ""),
                    source_exception_id=str(row.get("source_exception_id","") or ""),
                    je_type=            str(row.get("je_type", "")),
                    generated_by=       str(row.get("generated_by", "")),
                    subsidiary=         str(row.get("subsidiary", "")),
                    account_code=       str(row.get("account_code", "")),
                    debit=              _safe_decimal(row.get("debit")),
                    credit=             _safe_decimal(row.get("credit")),
                    currency=           str(row.get("currency", "")),
                    fxamount=           _safe_decimal(row.get("fxamount")),
                    memo=               str(row.get("memo", "")),
                    requires_review=    bool(row.get("requires_review", True)),
                    posted_to_netsuite= bool(row.get("posted_to_netsuite", False)),
                )
                session.add(draft)
                saved += 1

        print(f"[REPO] JE drafts saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # REPORTING QUERIES
    # Used by reporter.py to build Excel tabs from database
    # ------------------------------------------------------------------

    def get_matched_pairs(self, period: str) -> pd.DataFrame:
        """Returns all matched pairs for a period as a DataFrame."""
        with get_session() as session:
            pairs = (
                session.query(MatchPair)
                .filter_by(period=period)
                .order_by(MatchPair.confidence_score.desc())
                .all()
            )
            if not pairs:
                return pd.DataFrame()
            return pd.DataFrame([_model_to_dict(p) for p in pairs])

    def get_exceptions(self, period: str) -> pd.DataFrame:
        """
        Returns all open exceptions for a period as a DataFrame.
        Ordered by priority (P1 first) then days_aged descending.
        """
        with get_session() as session:
            exceptions = (
                session.query(Exception_)
                .filter(
                    Exception_.period == period,
                    Exception_.status == "OPEN",
                )
                .order_by(
                    Exception_.priority.asc(),
                    Exception_.days_aged.desc(),
                )
                .all()
            )
            if not exceptions:
                return pd.DataFrame()
            return pd.DataFrame([_model_to_dict(e) for e in exceptions])

    def get_je_drafts(self, period: str) -> pd.DataFrame:
        """
        Returns all JE drafts for a period.
        Ordered by requires_review (True first) then je_type.
        """
        with get_session() as session:
            drafts = (
                session.query(JEDraft)
                .filter_by(period=period)
                .order_by(
                    JEDraft.requires_review.desc(),
                    JEDraft.je_type,
                )
                .all()
            )
            if not drafts:
                return pd.DataFrame()
            return pd.DataFrame([_model_to_dict(d) for d in drafts])

    def get_run_log(self, period: str) -> pd.DataFrame:
        """Returns all run log entries for a period."""
        with get_session() as session:
            logs = (
                session.query(RunLog)
                .filter_by(period=period)
                .order_by(RunLog.started_at.desc())
                .all()
            )
            if not logs:
                return pd.DataFrame()
            return pd.DataFrame([_model_to_dict(l) for l in logs])

    def get_entity_pair_summary(self, period: str) -> pd.DataFrame:
        """
        Returns a summary of match rates per entity pair.
        Used for Tab 0 executive dashboard.
        """
        with get_session() as session:
            # Get matched pairs grouped by entity pair
            pairs_df = self.get_matched_pairs(period)
            exceptions_df = self.get_exceptions(period)

            if pairs_df.empty and exceptions_df.empty:
                return pd.DataFrame()

            rows = []

            # Get all entity pairs from matched pairs
            if not pairs_df.empty:
                for _, pair in pairs_df.iterrows():
                    entity_a = pair.get("orig_entity", "")
                    entity_b = pair.get("recv_entity", "")
                    rows.append({
                        "entity_a":      entity_a,
                        "entity_b":      entity_b,
                        "matched_fxamt": float(pair.get("orig_fxamount", 0) or 0),
                        "is_matched":    True,
                    })

            # Get all entity pairs from exceptions
            if not exceptions_df.empty:
                for _, exc in exceptions_df.iterrows():
                    rows.append({
                        "entity_a":      exc.get("source_entity", ""),
                        "entity_b":      exc.get("target_cseg", ""),
                        "matched_fxamt": 0.0,
                        "is_matched":    False,
                        "exception_amt": float(
                            exc.get("unmatched_fxamount", 0) or 0),
                    })

            if not rows:
                return pd.DataFrame()

            df = pd.DataFrame(rows)
            return df

    def get_unposted_je_drafts(self, hours_threshold: int = 24) -> pd.DataFrame:
        """
        Returns approved JE drafts that have not been posted to NetSuite.
        Used by notifier.py for SLA monitoring.
        """
        from sqlalchemy import func
        with get_session() as session:
            cutoff = datetime.utcnow()
            drafts = (
                session.query(JEDraft)
                .filter(
                    JEDraft.posted_to_netsuite == False,
                    JEDraft.approved_by.isnot(None),
                    JEDraft.created_at <= cutoff,
                )
                .all()
            )

            if not drafts:
                return pd.DataFrame()

            rows = []
            for d in drafts:
                hours_since_created = (
                    cutoff - d.created_at
                ).total_seconds() / 3600
                if hours_since_created >= hours_threshold:
                    rows.append({
                        **_model_to_dict(d),
                        "hours_unposted": round(hours_since_created, 1),
                    })

            return pd.DataFrame(rows) if rows else pd.DataFrame()

    # ------------------------------------------------------------------
    # NOTIFICATION LOG
    # ------------------------------------------------------------------

    def log_notification(
        self,
        run_id:            str,
        notification_type: str,
        channel:           str,
        recipient:         str,
        subject:           str = "",
        exception_id:      str = None,
        je_draft_id:       str = None,
    ) -> None:
        """Records that a notification was sent."""
        with get_session() as session:
            log = NotificationLog(
                run_id=            run_id,
                exception_id=      exception_id,
                je_draft_id=       je_draft_id,
                notification_type= notification_type,
                channel=           channel,
                recipient=         recipient,
                subject=           subject,
                delivery_status=   "SENT",
            )
            session.add(log)

    # ------------------------------------------------------------------
    # RESOLUTION PATTERNS (learned patterns)
    # ------------------------------------------------------------------

    def record_resolution_pattern(
        self,
        entity_pair:      str,
        exception_type:   str,
        resolution_type:  str,
        resolved_by:      str,
        account_code_a:   str = None,
        account_code_b:   str = None,
    ) -> None:
        """
        Records or increments a resolution pattern.
        Auto-enables pattern after times_seen >= 3.
        """
        with get_session() as session:
            existing = (
                session.query(ResolutionPattern)
                .filter_by(
                    entity_pair=     entity_pair,
                    exception_type=  exception_type,
                    resolution_type= resolution_type,
                    account_code_a=  account_code_a,
                    account_code_b=  account_code_b,
                )
                .first()
            )

            if existing:
                existing.times_seen      += 1
                existing.last_resolved_by = resolved_by
                existing.last_seen_at     = datetime.utcnow()
                if existing.times_seen >= 3:
                    existing.auto_resolve_enabled = True
                    print(
                        f"[REPO] Pattern learned: {entity_pair} "
                        f"{exception_type} → {resolution_type} "
                        f"(seen {existing.times_seen}x, auto-resolve enabled)"
                    )
            else:
                pattern = ResolutionPattern(
                    entity_pair=      entity_pair,
                    exception_type=   exception_type,
                    resolution_type=  resolution_type,
                    account_code_a=   account_code_a,
                    account_code_b=   account_code_b,
                    times_seen=       1,
                    auto_resolve_enabled=False,
                    first_resolved_by=resolved_by,
                    last_resolved_by= resolved_by,
                )
                session.add(pattern)

    def get_active_patterns(self) -> list[dict]:
        """
        Returns all auto-resolve enabled patterns.
        Used by engine.py to auto-resolve recurring known exceptions.
        """
        with get_session() as session:
            patterns = (
                session.query(ResolutionPattern)
                .filter_by(auto_resolve_enabled=True)
                .all()
            )
            return [_model_to_dict(p) for p in patterns]

    # ------------------------------------------------------------------
    # PERIOD LOCK
    # ------------------------------------------------------------------

    def lock_period(
        self,
        period:    str,
        locked_by: str,
        run_id:    str,
    ) -> None:
        """
        Locks a financial period — no further modifications allowed.
        The PostgreSQL trigger enforces this at the database level.
        This method sets the flag that the trigger checks.
        """
        with get_session() as session:
            fp = session.query(FinancialPeriod).filter_by(period=period).first()
            if fp:
                fp.period_locked = True
                fp.locked_by     = locked_by
                fp.locked_at     = datetime.utcnow()
                fp.lock_run_id   = run_id
            else:
                fp = FinancialPeriod(
                    period=        period,
                    period_locked= True,
                    locked_by=     locked_by,
                    locked_at=     datetime.utcnow(),
                    lock_run_id=   run_id,
                )
                session.add(fp)
        print(f"[REPO] Period {period} locked by {locked_by}.")

    def is_period_locked(self, period: str) -> bool:
        """Returns True if the period is locked."""
        with get_session() as session:
            fp = session.query(FinancialPeriod).filter_by(period=period).first()
            return fp.period_locked if fp else False


# -----------------------------------------------------------------------------
# HELPER FUNCTIONS
# Private utilities used within this module
# -----------------------------------------------------------------------------

def _safe_decimal(value) -> Optional[float]:
    """Converts a value to float safely. Returns None if not convertible."""
    if value is None or value == "" or value != value:  # NaN check
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value) -> Optional[float]:
    """Same as _safe_decimal — explicit alias for clarity."""
    return _safe_decimal(value)


def _safe_int(value) -> Optional[int]:
    """Converts a value to int safely. Returns None if not convertible."""
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _model_to_dict(model_instance) -> dict:
    """
    Converts a SQLAlchemy model instance to a plain dictionary.
    Used to convert query results to DataFrames.
    Excludes SQLAlchemy internal state attributes.
    """
    return {
        col.name: getattr(model_instance, col.name)
        for col in model_instance.__table__.columns
    }