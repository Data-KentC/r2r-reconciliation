# =============================================================================
# R2R INTERCOMPANY RECONCILIATION ENGINE
# src/matching/keygen.py
#
# What this file does:
#   Generates two types of keys for every IC transaction row:
#
#   1. row_id (unique per row):
#      SHA-256 of internalid + local_entity
#      Guarantees every row has a unique identity regardless of content.
#      Used as the primary key in the database.
#
#   2. match_group_key (symmetric — same for both sides of a transaction):
#      SHA-256 of sorted([entity_a, entity_b]) + period + currency + round(fxamount, 0)
#      Lexicographic sort of entities ensures ANTH-SG→ANTH-JP produces
#      the same key as ANTH-JP→ANTH-SG.
#      Used by Tier 2 matching to group candidate pairs.
#
#   3. tranid_key (for Tier 1 — AICJEs only):
#      The raw tranid value normalised to uppercase with whitespace stripped.
#      Two rows with the same tranid_key are guaranteed to be the same AICJE.
#
# Collision handling (SHA-256 collision on recurring fixed charges):
#   If two different transactions produce the same match_group_key
#   (e.g. same monthly $10,000 charge twice), the engine detects this
#   via bucket count comparison and handles it in exact.py.
#   The row_id remains unique — collision only affects grouping, not identity.
#
# How other files use it:
#   from src.matching.keygen import assign_keys
#   df = assign_keys(ic_df)
# =============================================================================

import hashlib
import re
from typing import Optional

import pandas as pd

from src.config import config


# -----------------------------------------------------------------------------
# CONSTANTS
# -----------------------------------------------------------------------------

NS = config.netsuite.fields


# -----------------------------------------------------------------------------
# KEY GENERATION HELPERS
# -----------------------------------------------------------------------------

def _sha256(value: str) -> str:
    """Returns a SHA-256 hex digest of a string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _extract_period(tran_date: str) -> str:
    """
    Extracts YYYY-MM from a transaction date string.
    Used in match_group_key to bound matching to the same period.

    Examples:
        "2026-06-15" → "2026-06"
        "2026-07-04" → "2026-07"  (timing gap case)
        "invalid"    → "0000-00"  (safe fallback)
    """
    try:
        cleaned = str(tran_date).strip()[:7]  # Take first 7 chars: "YYYY-MM"
        if re.match(r"^\d{4}-\d{2}$", cleaned):
            return cleaned
    except Exception:
        pass
    return "0000-00"


def _normalise_tranid(tranid: str) -> str:
    """
    Normalises a tranid for Tier 1 matching.
    Strips whitespace, converts to uppercase.

    Returns empty string if tranid is null/empty.
    """
    if not tranid or str(tranid).strip().lower() in ("nan", "", "none"):
        return ""
    return str(tranid).strip().upper()


def _round_fxamount(fxamount) -> int:
    """
    Rounds fxamount to 0 decimal places for match key construction.

    Why round to 0dp:
        Solves the 10000.00 vs 9999.99 problem — minor FX rounding
        differences between entities place both amounts in the same
        candidate bucket. The strict 1% / $100 check in Tier 3 then
        determines if the variance is acceptable.

    Returns 0 if fxamount is null or unparseable.
    """
    try:
        return int(round(abs(float(fxamount)), 0))
    except (TypeError, ValueError):
        return 0


# -----------------------------------------------------------------------------
# ROW IDENTITY KEY
# Unique per row — never collides
# -----------------------------------------------------------------------------

def _generate_row_id(
    internalid:         str,
    local_entity:        str,
    linesequencenumber:  str,
) -> str:
    """
    Generates a unique row identity key.

    Formula: SHA-256(internalid + "|" + local_entity + "|" + linesequencenumber)

    Why this combination:
        internalid alone is not unique — every line of a multi-line
        journal entry shares the SAME internalid (e.g. a 4-line AICJE
        has one internalid across all 4 debit/credit lines).
        internalid + local_entity is still not unique for the same
        reason — multiple lines of one JE belong to the same entity.
        Adding linesequencenumber guarantees uniqueness per GL line,
        which is what row_id is meant to identify (one row = one line).
        local_entity is still included so the same internalid used by
        two different entities (rare, but possible in NetSuite) never
        collides either.
    """
    value = (
        f"{str(internalid).strip()}"
        f"|{str(local_entity).strip()}"
        f"|{str(linesequencenumber).strip()}"
    )
    return _sha256(value)


# -----------------------------------------------------------------------------
# MATCH GROUP KEY
# Symmetric — same for both sides of an IC transaction
# Used for Tier 2 candidate grouping
# -----------------------------------------------------------------------------

def _generate_match_group_key(
    local_entity:   str,
    counterparty:   str,
    tran_date:      str,
    currency:       str,
    fxamount,
) -> str:
    """
    Generates a symmetric match group key for Tier 2 candidate grouping.

    Formula: SHA-256(sorted_entities + period + currency + rounded_amount)

    Symmetric design:
        sorted([ANTH-SG, ANTH-JP]) → ["ANTH-JP", "ANTH-SG"] always
        ANTH-SG posting to ANTH-JP produces the same key as
        ANTH-JP posting to ANTH-SG.

    Collision scenario:
        Two transactions between the same entities, same period, same
        currency, same rounded amount will produce the same key.
        This is handled in exact.py by bucket count comparison.
    """
    # Lexicographic sort guarantees symmetry
    entities_sorted = sorted([
        str(local_entity).strip(),
        str(counterparty).strip(),
    ])

    period          = _extract_period(tran_date)
    currency_clean  = str(currency).strip().upper()
    amount_rounded  = _round_fxamount(fxamount)

    value = (
        f"{'|'.join(entities_sorted)}"
        f"|{period}"
        f"|{currency_clean}"
        f"|{amount_rounded}"
    )

    return _sha256(value)


# -----------------------------------------------------------------------------
# MAIN KEY ASSIGNMENT FUNCTION
# Called by matching/engine.py before Tier 2 matching begins
# -----------------------------------------------------------------------------

def assign_keys(ic_df: pd.DataFrame) -> pd.DataFrame:
    """
    Assigns row_id, match_group_key, and tranid_key to every IC row.

    Adds three new columns:
        row_id:           Unique identity key per row (SHA-256)
        match_group_key:  Symmetric candidate grouping key (SHA-256)
        tranid_key:       Normalised tranid for Tier 1 (empty if not AICJE)

    Args:
        ic_df: Classified IC DataFrame from classifier.py

    Returns:
        ic_df with three new key columns added.
    """
    df = ic_df.copy()

    print("[KEYGEN] Generating row identity keys...")

    # --- Row ID: unique per row ---
    df["row_id"] = df.apply(
        lambda row: _generate_row_id(
            row[NS.internal_id],
            row["local_entity"],
            row[NS.line_sequence],
        ),
        axis=1,
    )

    # --- tranid key: Tier 1 exact match (AICJEs) ---
    df["tranid_key"] = df[NS.tran_id].apply(_normalise_tranid)

    # --- Match group key: Tier 2 symmetric grouping ---
    df["match_group_key"] = df.apply(
        lambda row: _generate_match_group_key(
            local_entity=  row["local_entity"],
            counterparty=  row["ic_counterparty"],
            tran_date=     row[NS.tran_date],
            currency=      row[NS.currency],
            fxamount=      row[NS.fx_amount],
        ),
        axis=1,
    )

    # --- Validation: row_id must be unique ---
    duplicate_ids = df["row_id"].duplicated().sum()
    if duplicate_ids > 0:
        print(
            f"[KEYGEN] WARNING: {duplicate_ids} duplicate row_ids detected. "
            f"This may indicate duplicate rows in the NetSuite export. "
            f"Investigate before proceeding."
        )

    # --- Summary ---
    has_tranid      = (df["tranid_key"] != "").sum()
    has_match_key   = (df["match_group_key"] != "").sum()
    unique_groups   = df["match_group_key"].nunique()

    print(
        f"[KEYGEN] Key generation complete:"
        f"\n  Rows processed:       {len(df)}"
        f"\n  Rows with tranid:     {has_tranid}  (eligible for Tier 1)"
        f"\n  Rows with match key:  {has_match_key}"
        f"\n  Unique match groups:  {unique_groups}"
        f"\n  Avg rows per group:   "
        f"{has_match_key / unique_groups:.1f}" if unique_groups > 0
        else "\n  Unique match groups:  0"
    )

    return df


# -----------------------------------------------------------------------------
# COLLISION DETECTOR
# Called by exact.py after Tier 2 grouping to detect collision buckets
# -----------------------------------------------------------------------------

def detect_collisions(
    df_source: pd.DataFrame,
    df_dest:   pd.DataFrame,
) -> pd.DataFrame:
    """
    Detects SHA-256 match key collisions between source and destination pools.

    A collision occurs when a match_group_key bucket contains more rows
    on one side than the other — indicating multiple different transactions
    produced the same hash.

    Example collision:
        ANTH-SG → ANTH-JP: $10,000 management fee (key: abc123)
        ANTH-SG → ANTH-JP: $10,000 IT recharge    (key: abc123)
        ANTH-JP has only 1 matching entry          (key: abc123)
        → 2 source rows, 1 dest row → collision detected

    Returns:
        DataFrame with columns:
            match_group_key:  The colliding hash
            source_count:     Number of source rows in this bucket
            dest_count:       Number of dest rows in this bucket
            is_collision:     True if counts differ
    """
    source_counts = (
        df_source.groupby("match_group_key")
        .size()
        .reset_index(name="source_count")
    )
    dest_counts = (
        df_dest.groupby("match_group_key")
        .size()
        .reset_index(name="dest_count")
    )

    merged = source_counts.merge(dest_counts, on="match_group_key", how="outer")
    merged["source_count"] = merged["source_count"].fillna(0).astype(int)
    merged["dest_count"]   = merged["dest_count"].fillna(0).astype(int)
    merged["is_collision"] = merged["source_count"] != merged["dest_count"]

    collision_count = merged["is_collision"].sum()
    if collision_count > 0:
        print(
            f"[KEYGEN] COLLISION DETECTED: {collision_count} match group keys "
            f"have asymmetric row counts. These buckets will be routed to "
            f"manual review rather than auto-matched."
        )

    return merged


# -----------------------------------------------------------------------------
# STANDALONE TEST
# Run directly to test key generation against synthetic data:
#   python src/matching/keygen.py
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

    print("Running ingestion...")
    ic_df, meta = ingest(synthetic_path)

    print("\nRunning key generation...")
    keyed_df = assign_keys(ic_df)

    print("\nSAMPLE OF KEYS:")
    print(keyed_df[[
        "local_entity",
        "ic_counterparty",
        "row_id",
        "tranid_key",
        "match_group_key",
    ]].head(10).to_string())

    print(f"\nUnique row_ids:         {keyed_df['row_id'].nunique()}")
    print(f"Total rows:             {len(keyed_df)}")
    print(f"Rows with tranid:       {(keyed_df['tranid_key'] != '').sum()}")
    print(f"Unique match groups:    {keyed_df['match_group_key'].nunique()}")
