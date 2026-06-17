# =============================================================================
# R2R INTERCOMPANY RECONCILIATION ENGINE
# src/matching/engine.py
#
# What this file does:
#   Orchestrates the complete 5-tier matching pipeline.
#   This is the single entry point that run_pipeline.py calls.
#   It sequences all tiers, assembles results, and returns:
#     - All matched pairs (combined from Tiers 1-4)
#     - All exceptions (orphans enriched with Tier 5 LLM suggestions)
#     - A MatchResult summary for the run log
#
#   Processing sequence:
#     1. Assign match keys (keygen.py)
#     2. Tier 1: tranid exact join
#     3. Tier 2: SHA-256 hash match
#     4. Tier 3: FX tolerance + timing gap
#     5. Tier 4: Subset sum (1-to-many)
#     6. Tier 5: LLM orphan suggestion
#     7. Classify all remaining rows as exceptions
#     8. Compute STP rate and compare to previous run
#     9. Return MatchResult
#
#   STP rate monitoring:
#     Straight-Through Processing rate = matched / total IC rows.
#     If STP drops >15% vs previous run, a high-severity alert fires.
#     Previous run STP is read from the database via repository.py.
#
# How other files use it:
#   from src.matching.engine import run_matching_engine
#   result = run_matching_engine(ic_df, meta, run_id)
# =============================================================================

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import uuid

import pandas as pd

from src.config import config
from src.matching.keygen     import assign_keys
from src.matching.exact      import match_tier1, match_tier2
from src.matching.tolerance  import match_tier3
from src.matching.subset_sum import match_tier4
from src.matching.llm_matcher import suggest_counterparties
from src.ingestion.ingestor  import IngestionMeta


# -----------------------------------------------------------------------------
# MATCH RESULT
# Summary of a completed matching run — written to run_log table
# -----------------------------------------------------------------------------

@dataclass
class MatchResult:
    """
    Complete results from one matching engine run.
    Written to run_log in the database and embedded in Excel Tab 0.
    """
    run_id:              str
    period:              str
    started_at:          datetime
    completed_at:        Optional[datetime]

    # Input counts
    total_ic_rows:       int

    # Tier match counts
    tier1_matches:       int
    tier2_matches:       int
    tier3_matches:       int
    tier4_matches:       int
    llm_suggestions:     int

    # Exception counts
    total_exceptions:    int
    orphans:             int
    timing_gaps:         int
    amount_mismatches:   int
    account_mismatches:  int
    currency_mismatches: int
    other_exceptions:    int

    # STP metrics
    stp_rate:            float    # 0.0-1.0
    stp_previous_run:    float    # 0.0-1.0 from last run
    stp_delta:           float    # positive = improvement
    stp_alert_fired:     bool     # True if drop > threshold

    # DataFrames — the actual output
    matched_pairs:       pd.DataFrame = field(default_factory=pd.DataFrame)
    exceptions:          pd.DataFrame = field(default_factory=pd.DataFrame)

    # File metadata
    input_file_md5:      str = ""
    config_version:      str = ""
    priority_by_type:    dict = field(default_factory=dict)

# -----------------------------------------------------------------------------
# EXCEPTION CLASSIFIER
# Classifies remaining unmatched rows into exception types
# -----------------------------------------------------------------------------

def _classify_exceptions(
    orphan_df:    pd.DataFrame,
    run_id:       str,
    period:       str,
) -> pd.DataFrame:
    """
    Classifies all unmatched rows into exception types.

    Exception types (from config.yaml):
        ORPHAN:           No counterpart found anywhere
        TIMING_GAP:       Would match but period difference > ±1
        AMOUNT_MISMATCH:  Close match but amount outside tolerance
        ACCOUNT_MISMATCH: Same amount, different account codes
        CURRENCY_MISMATCH: Currencies differ between entities
        CSEG_INVALID:     CSEG tag exists but unrecognised
        PARTIAL_REVERSAL: Reversal leaves non-zero net

    Priority rules (from config.yaml):
        P1: Amount > $500K or aged > 30 days
        P2: Amount $100K-$500K
        P3: Everything else

    Adds these columns:
        exception_type       str
        priority             str   P1 | P2 | P3
        days_aged            int
        aging_bucket         str   0-30 | 31-60 | 61-90 | 90+
        run_id               str
        period               str
        first_seen           str   (populated by repository on insert)
        occurrence_count     int   (populated by repository on insert)
        status               str   OPEN
    """
    df = orphan_df.copy()

    if len(df) == 0:
        return df

    ns  = config.netsuite.fields
    exc = config.exceptions

    # --- Exception type classification ---
    def _determine_exception_type(row) -> str:
        ic_source = str(row.get("ic_source", "")).strip()
        cseg      = str(row.get(ns.cseg_ic, "")).strip()
        memo_id   = str(row.get("ic_source", "")).strip()

        if ic_source == "CSEG" and cseg not in config.valid_entity_codes:
            return "CSEG_INVALID"
        if row.get("is_partial_reversal", False):
            return "PARTIAL_REVERSAL"
        if row.get(ns.currency, "") not in config.valid_currencies:
            return "CURRENCY_MISMATCH"
        # Default: genuine orphan with no counterpart
        return "ORPHAN"

    df["exception_type"] = df.apply(_determine_exception_type, axis=1)

    # --- Priority calculation ---
    def _determine_priority(row) -> str:
        try:
            amount_usd = abs(float(row.get(ns.fx_amount, 0) or 0))
        except (TypeError, ValueError):
            amount_usd = 0.0

        if amount_usd >= exc.priority_rules["p1_threshold_usd"]:
            return "P1"
        elif amount_usd >= exc.priority_rules["p2_threshold_usd"]:
            return "P2"
        else:
            return "P3"

    df["priority"] = df.apply(_determine_priority, axis=1)

    # --- Aging calculation ---
    today = datetime.utcnow().date()

    def _days_aged(tran_date_str: str) -> int:
        try:
            tran_date = datetime.strptime(
                str(tran_date_str).strip()[:10], "%Y-%m-%d"
            ).date()
            return max(0, (today - tran_date).days)
        except Exception:
            return 0

    df["days_aged"] = df[ns.tran_date].apply(_days_aged)

    # Override priority for aged items
    aged_mask = df["days_aged"] > exc.priority_rules["aged_override_days"]
    df.loc[aged_mask, "priority"] = "P1"

    # Aging bucket
    def _aging_bucket(days: int) -> str:
        if days <= 30:  return "0-30"
        if days <= 60:  return "31-60"
        if days <= 90:  return "61-90"
        return "90+"

    df["aging_bucket"]      = df["days_aged"].apply(_aging_bucket)
    df["run_id"]            = run_id
    df["period"]            = period
    df["status"]            = "OPEN"
    df["occurrence_count"]  = 1  # Repository increments on re-run

    # --- Rename key columns for exceptions output ---
    df["source_entity"]  = df["local_entity"]
    df["target_cseg"]    = df[ns.cseg_ic].fillna("")
    df["unmatched_fxamount"] = df[ns.fx_amount].apply(
        lambda x: float(x) if x else 0.0
    )
    df["unmatched_currency"] = df[ns.currency]

    return df


# -----------------------------------------------------------------------------
# STP RATE CALCULATOR
# Computes Straight-Through Processing rate and detects anomalies
# -----------------------------------------------------------------------------

def _compute_stp(
    total_ic_rows:    int,
    total_matched:    int,
    previous_stp:     float,
) -> tuple[float, float, bool]:
    """
    Computes STP rate and checks for material degradation.

    STP rate = matched rows / total IC rows.
    Alert fires if STP drops > stp_alert_drop_pct vs previous run.

    Returns:
        (stp_rate, stp_delta, alert_fired)
    """
    if total_ic_rows == 0:
        return 0.0, 0.0, False

    stp_rate  = total_matched / total_ic_rows
    stp_delta = stp_rate - previous_stp

    alert_threshold = config.governance.stp_alert_drop_pct / 100.0
    alert_fired = (
        previous_stp > 0 and
        stp_delta < -alert_threshold
    )

    if alert_fired:
        print(
            f"\n[ENGINE] ⚠️  STP ALERT: Rate dropped from "
            f"{previous_stp:.1%} to {stp_rate:.1%} "
            f"(delta: {stp_delta:.1%}). "
            f"Threshold: {alert_threshold:.0%}. "
            f"Investigate upstream data quality."
        )

    return stp_rate, stp_delta, alert_fired


# -----------------------------------------------------------------------------
# COMBINE MATCHED PAIRS
# Merges results from all tiers into a single DataFrame
# -----------------------------------------------------------------------------

def _combine_matched_pairs(
    tier_results: list[pd.DataFrame],
    run_id:       str,
    period:       str,
) -> pd.DataFrame:
    """
    Combines matched pair DataFrames from all tiers.
    Adds run_id and period for database traceability.
    """
    non_empty = [df for df in tier_results if df is not None and len(df) > 0]

    if not non_empty:
        return pd.DataFrame()

    combined = pd.concat(non_empty, ignore_index=True)
    combined["run_id"] = run_id
    combined["period"] = period
    combined["match_id"] = [str(uuid.uuid4()) for _ in range(len(combined))]

    return combined


# -----------------------------------------------------------------------------
# MAIN MATCHING ENGINE
# Single entry point called by run_pipeline.py
# -----------------------------------------------------------------------------

def run_matching_engine(
    ic_df:         pd.DataFrame,
    meta:          IngestionMeta,
    run_id:        str,
    previous_stp:  float = 0.0,
) -> MatchResult:
    """
    Runs the complete 5-tier matching engine on classified IC transactions.

    Args:
        ic_df:        IC transactions from ingestor.py
        meta:         Ingestion metadata (file hash, period, counts)
        run_id:       Unique identifier for this pipeline run
        previous_stp: STP rate from previous run (for anomaly detection)

    Returns:
        MatchResult with all matched pairs, exceptions, and run statistics.
    """
    started_at = datetime.utcnow()
    period     = meta.period

    print("\n" + "=" * 60)
    print("MATCHING ENGINE")
    print("=" * 60)
    print(f"Run ID:    {run_id}")
    print(f"Period:    {period}")
    print(f"IC rows:   {len(ic_df)}")
    print(f"Prev STP:  {previous_stp:.1%}")

    # ------------------------------------------------------------------
    # Step 1: Assign match keys
    # ------------------------------------------------------------------
    keyed_df = assign_keys(ic_df)

    # ------------------------------------------------------------------
    # Step 2: Tier 1 — tranid exact join
    # ------------------------------------------------------------------
    t1_pairs, after_t1 = match_tier1(keyed_df)

    # ------------------------------------------------------------------
    # Step 3: Tier 2 — SHA-256 hash match
    # ------------------------------------------------------------------
    t2_pairs, after_t2 = match_tier2(after_t1)

    # ------------------------------------------------------------------
    # Step 4: Tier 3 — FX tolerance + timing gap
    # ------------------------------------------------------------------
    t3_pairs, after_t3 = match_tier3(after_t2)

    # ------------------------------------------------------------------
    # Step 5: Tier 4 — Subset sum (1-to-many)
    # ------------------------------------------------------------------
    t4_pairs, after_t4 = match_tier4(after_t3)

    # ------------------------------------------------------------------
    # Step 6: Tier 5 — LLM orphan suggestion
    # Does NOT match — enriches orphans with counterparty suggestions
    # ------------------------------------------------------------------
    enriched_orphans = suggest_counterparties(after_t4)

    # ------------------------------------------------------------------
    # Step 7: Classify exceptions
    # ------------------------------------------------------------------
    exceptions_df = _classify_exceptions(
        orphan_df= enriched_orphans,
        run_id=    run_id,
        period=    period,
    )

    # ------------------------------------------------------------------
    # Step 8: Combine all matched pairs
    # ------------------------------------------------------------------
    matched_pairs_df = _combine_matched_pairs(
        tier_results= [t1_pairs, t2_pairs, t3_pairs, t4_pairs],
        run_id=       run_id,
        period=       period,
    )

    # ------------------------------------------------------------------
    # Step 9: Compute STP rate
    # Count matched rows (each pair = 2 original rows matched)
    # ------------------------------------------------------------------
    total_ic_rows  = len(ic_df)
    tier1_count    = len(t1_pairs) if t1_pairs is not None else 0
    tier2_count    = len(t2_pairs) if t2_pairs is not None else 0
    tier3_count    = len(t3_pairs) if t3_pairs is not None else 0
    tier4_count    = len(t4_pairs) if t4_pairs is not None else 0

    # Each pair row represents 2 matched GL rows
    total_matched_rows = (tier1_count + tier2_count + tier3_count + tier4_count) * 2
    total_matched_rows = min(total_matched_rows, total_ic_rows)

    stp_rate, stp_delta, stp_alert = _compute_stp(
        total_ic_rows= total_ic_rows,
        total_matched= total_matched_rows,
        previous_stp=  previous_stp,
    )

    # ------------------------------------------------------------------
    # Step 10: Count exception types
    # ------------------------------------------------------------------
    
    exc_counts = {}
    priority_by_type = {}
    if len(exceptions_df) > 0:
        exc_counts = exceptions_df["exception_type"].value_counts().to_dict()
        priority_by_type = (
            exceptions_df.groupby("exception_type")["priority"]
            .value_counts()
            .unstack(fill_value=0)
            .reindex(columns=["P1", "P2", "P3"], fill_value=0)
            .apply(lambda r: f"P1:{r['P1']} / P2:{r['P2']} / P3:{r['P3']}", axis=1)
            .to_dict()
        )
    
    llm_suggested = 0
    if len(enriched_orphans) > 0:
        llm_suggested = (
            enriched_orphans.get("llm_routing", pd.Series())
            == "LLM_SUGGESTED"
        ).sum()

    # ------------------------------------------------------------------
    # Step 11: Build and return MatchResult
    # ------------------------------------------------------------------
    completed_at = datetime.utcnow()

    result = MatchResult(
        run_id=           run_id,
        period=           period,
        started_at=       started_at,
        completed_at=     completed_at,
        total_ic_rows=    total_ic_rows,
        tier1_matches=    tier1_count,
        tier2_matches=    tier2_count,
        tier3_matches=    tier3_count,
        tier4_matches=    tier4_count,
        llm_suggestions=  int(llm_suggested),
        total_exceptions= len(exceptions_df),
        orphans=          exc_counts.get("ORPHAN", 0),
        timing_gaps=      exc_counts.get("TIMING_GAP", 0),
        amount_mismatches=exc_counts.get("AMOUNT_MISMATCH", 0),
        account_mismatches=exc_counts.get("ACCOUNT_MISMATCH", 0),
        currency_mismatches=exc_counts.get("CURRENCY_MISMATCH", 0),
        other_exceptions= len(exceptions_df) - sum(exc_counts.values()),
        stp_rate=         stp_rate,
        stp_previous_run= previous_stp,
        stp_delta=        stp_delta,
        stp_alert_fired=  stp_alert,
        matched_pairs=    matched_pairs_df,
        exceptions=       exceptions_df,
        input_file_md5=   meta.file_md5,
    )

    # ------------------------------------------------------------------
    # Final summary
    # ------------------------------------------------------------------
    runtime = (completed_at - started_at).total_seconds()

    print("\n" + "=" * 60)
    print("MATCHING COMPLETE")
    print("=" * 60)
    print(f"  Total IC rows:    {total_ic_rows}")
    print(f"  Tier 1 matches:   {tier1_count}  pairs")
    print(f"  Tier 2 matches:   {tier2_count}  pairs")
    print(f"  Tier 3 matches:   {tier3_count}  pairs")
    print(f"  Tier 4 matches:   {tier4_count}  pairs (subset sum)")
    print(f"  LLM suggestions:  {int(llm_suggested)}")
    print(f"  Exceptions:       {len(exceptions_df)}")
    print(f"  STP rate:         {stp_rate:.1%}")
    print(f"  STP delta:        {stp_delta:+.1%}")
    print(f"  STP alert:        {'YES ⚠️' if stp_alert else 'No'}")
    print(f"  Runtime:          {runtime:.1f}s")
    print("=" * 60 + "\n")

    return result


# -----------------------------------------------------------------------------
# STANDALONE TEST
# Run directly to test full engine against synthetic data:
#   python src/matching/engine.py
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

    from src.ingestion.ingestor import ingest

    synthetic_path = os.path.join(
        os.path.dirname(__file__),
        "../../data/synthetic/synthetic_gl_jun2026.csv"
    )

    if not os.path.exists(synthetic_path):
        print(
            "Synthetic data not found. Run this first:\n"
            "  python tests/synthetic/generate_synthetic.py"
        )
        sys.exit(1)

    # Run ingestion
    ic_df, meta = ingest(synthetic_path)

    # Run matching engine
    test_run_id = str(uuid.uuid4())
    result = run_matching_engine(
        ic_df=        ic_df,
        meta=         meta,
        run_id=       test_run_id,
        previous_stp= 0.0,
    )

    print("\nMATCH RESULT SUMMARY")
    print(f"Run ID:           {result.run_id}")
    print(f"Period:           {result.period}")
    print(f"STP Rate:         {result.stp_rate:.1%}")
    print(f"Matched pairs:    {len(result.matched_pairs)}")
    print(f"Exceptions:       {len(result.exceptions)}")

    if len(result.matched_pairs) > 0:
        print("\nMatched pairs sample:")
        print(result.matched_pairs[[
            "orig_entity", "recv_entity",
            "orig_fxamount", "recv_fxamount",
            "match_tier", "confidence_score",
        ]].head(10).to_string())

    if len(result.exceptions) > 0:
        print("\nExceptions sample:")
        print(result.exceptions[[
            "source_entity", "target_cseg",
            "exception_type", "priority",
            "unmatched_fxamount", "days_aged",
            "llm_suggested_entity", "llm_routing",
        ]].head(10).to_string())
