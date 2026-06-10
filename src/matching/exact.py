# =============================================================================
# R2R INTERCOMPANY RECONCILIATION ENGINE
# src/matching/exact.py
#
# What this file does:
#   Implements Tier 1 and Tier 2 matching — the two highest-confidence
#   deterministic matching methods.
#
#   Tier 1 — tranid exact join:
#     Matches rows that share the same tranid_key.
#     Only applies to Advanced Intercompany Journal Entries (AICJEs)
#     where NetSuite generates a shared document number on both sides.
#     Confidence score: 100 (highest possible).
#     No false positives possible — tranid is system-generated.
#
#   Tier 2 — SHA-256 hash match:
#     Matches rows that share the same match_group_key.
#     Applies to manual JEs where no shared tranid exists.
#     Includes collision detection — if a hash bucket has unequal
#     row counts on both sides, it is routed to manual review.
#     Confidence score: 85-95 depending on secondary checks.
#
# How other files use it:
#   from src.matching.exact import match_tier1, match_tier2
#   matched_pairs, remaining = match_tier1(keyed_df)
#   more_pairs, remaining    = match_tier2(remaining)
#
# Output — matched_pairs DataFrame columns:
#   orig_internalid     str   Originator internal ID
#   orig_entity         str   Originator entity code
#   orig_trandate       str   Originator transaction date
#   orig_currency       str   Originator currency
#   orig_fxamount       float Originator FX amount
#   orig_account_code   str   Originator account code
#   orig_account_name   str   Originator account name
#   recv_internalid     str   Receiver internal ID
#   recv_entity         str   Receiver entity code
#   recv_trandate       str   Receiver transaction date
#   recv_currency       str   Receiver currency
#   recv_fxamount       float Receiver FX amount
#   recv_account_code   str   Receiver account code
#   recv_account_name   str   Receiver account name
#   match_tier          str   "TIER1_TRANID" | "TIER2_HASH"
#   confidence_score    int   0-100
#   is_entity_matched   bool  SOX boolean — entities correctly paired
#   is_currency_matched bool  SOX boolean — currencies match
#   is_fxamount_exact   bool  SOX boolean — fxamounts exactly equal
#   is_period_matched   bool  SOX boolean — same posting period
#   variance_fxamount   float orig_fxamount - recv_fxamount
#   variance_usd        float estimated USD variance (for reporting)
#   tolerance_reason    str   "NONE" for Tier 1/2 exact matches
#   group_pairing_id    str   UUID for subset sum groups (empty here)
# =============================================================================

import uuid
from typing import Tuple, List

import pandas as pd
import numpy as np

from src.config import config
from src.matching.keygen import detect_collisions


# -----------------------------------------------------------------------------
# CONSTANTS
# -----------------------------------------------------------------------------

NS = config.netsuite.fields


# -----------------------------------------------------------------------------
# MATCH PAIR BUILDER
# Constructs a standardised matched pair row from two GL rows
# -----------------------------------------------------------------------------

def _build_match_pair(
    orig:       pd.Series,
    recv:       pd.Series,
    match_tier: str,
    confidence: int,
) -> dict:
    """
    Builds a standardised matched pair dictionary from two GL rows.

    The originator is determined by:
    1. Functional currency match (Option A — accounting-correct)
    2. Alphabetical sort (Option B — deterministic fallback)

    SOX boolean columns are derived here — auditors can verify
    each boolean independently without reading the matching code.
    """
    # Determine originator vs receiver
    # Option A: entity whose functional currency matches transaction currency
    orig_functional = config.entities.get(
        orig["local_entity"], None
    )
    recv_functional = config.entities.get(
        recv["local_entity"], None
    )

    orig_fc = orig_functional.functional_currency if orig_functional else ""
    recv_fc = recv_functional.functional_currency if recv_functional else ""

    transaction_currency = str(orig[NS.currency]).strip()

    # Swap if receiver's functional currency matches — they are the initiator
    if recv_fc == transaction_currency and orig_fc != transaction_currency:
        orig, recv = recv, orig

    # If both or neither match (third-currency billing), use alphabetical
    if orig_fc != transaction_currency and recv_fc != transaction_currency:
        entities = sorted([orig["local_entity"], recv["local_entity"]])
        if orig["local_entity"] != entities[0]:
            orig, recv = recv, orig

    # --- Variance calculation ---
    orig_fx = float(orig[NS.fx_amount]) if orig[NS.fx_amount] else 0.0
    recv_fx = float(recv[NS.fx_amount]) if recv[NS.fx_amount] else 0.0
    variance_fx = abs(orig_fx - recv_fx)

    # USD variance approximation
    # In production: use actual exchange rates from config or rate table
    # For prototype: variance_usd = variance_fxamount (assumes USD transaction)
    variance_usd = variance_fx

    # --- SOX boolean decomposition ---
    is_entity_matched   = (
        str(orig["ic_counterparty"]).strip() == str(recv["local_entity"]).strip()
        and
        str(recv["ic_counterparty"]).strip() == str(orig["local_entity"]).strip()
    )
    is_currency_matched = (
        str(orig[NS.currency]).strip() == str(recv[NS.currency]).strip()
    )
    is_fxamount_exact   = abs(variance_fx) < 0.01  # Within 1 cent
    is_period_matched   = (
        str(orig[NS.tran_date])[:7] == str(recv[NS.tran_date])[:7]
    )

    # --- Confidence score ---
    # Base score passed in, adjusted by SOX boolean checks
    score = confidence
    if not is_entity_matched:   score -= 20
    if not is_currency_matched: score -= 15
    if not is_fxamount_exact:   score -= 10
    if not is_period_matched:   score -= 10
    score = max(0, min(100, score))  # Clamp to 0-100

    return {
        # Originator side
        "orig_internalid":   orig[NS.internal_id],
        "orig_entity":       orig["local_entity"],
        "orig_trandate":     orig[NS.tran_date],
        "orig_currency":     orig[NS.currency],
        "orig_fxamount":     orig_fx,
        "orig_account_code": orig["account_code"],
        "orig_account_name": orig["account_name"],
        "orig_row_id":       orig["row_id"],
        "orig_ic_source":    orig["ic_source"],

        # Receiver side
        "recv_internalid":   recv[NS.internal_id],
        "recv_entity":       recv["local_entity"],
        "recv_trandate":     recv[NS.tran_date],
        "recv_currency":     recv[NS.currency],
        "recv_fxamount":     recv_fx,
        "recv_account_code": recv["account_code"],
        "recv_account_name": recv["account_name"],
        "recv_row_id":       recv["row_id"],
        "recv_ic_source":    recv["ic_source"],

        # Match metadata
        "match_tier":          match_tier,
        "confidence_score":    score,
        "is_entity_matched":   is_entity_matched,
        "is_currency_matched": is_currency_matched,
        "is_fxamount_exact":   is_fxamount_exact,
        "is_period_matched":   is_period_matched,
        "variance_fxamount":   variance_fx,
        "variance_usd":        variance_usd,
        "tolerance_reason":    "NONE",
        "group_pairing_id":    "",
    }


# -----------------------------------------------------------------------------
# TIER 1: tranid EXACT JOIN
# Highest confidence — system-generated shared key
# -----------------------------------------------------------------------------

def match_tier1(
    df: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Tier 1 matching: tranid exact join for Advanced IC Journal Entries.

    Logic:
    - Group rows by tranid_key
    - For each group with exactly 2 rows from different entities: match them
    - Groups with 1 row or same-entity rows: pass to Tier 2
    - Groups with >2 rows: pass to Tier 2 (may be subset sum candidates)

    Returns:
        matched_pairs: DataFrame of confirmed Tier 1 matches
        remaining:     DataFrame of rows not matched in Tier 1
    """
    print("\n[TIER 1] Starting tranid exact matching...")

    # Only attempt Tier 1 on rows that have a tranid
    has_tranid = df["tranid_key"] != ""
    tier1_pool = df[has_tranid].copy()
    no_tranid  = df[~has_tranid].copy()

    matched_pairs = []
    matched_row_ids = set()

    if len(tier1_pool) == 0:
        print("[TIER 1] No rows with tranid found. Skipping Tier 1.")
        return pd.DataFrame(), df

    # Group by tranid_key
    for tranid_key, group in tier1_pool.groupby("tranid_key"):

        if len(group) < 2:
            # Only one side posted — cannot match
            continue

        # Get unique entities in this group
        entities_in_group = group["local_entity"].unique()

        if len(entities_in_group) < 2:
            # Both rows from same entity — not a valid IC pair
            continue

        # For exactly 2 rows from different entities: direct match
        if len(group) == 2:
            row_a = group.iloc[0]
            row_b = group.iloc[1]

            pair = _build_match_pair(
                orig=       row_a,
                recv=       row_b,
                match_tier= "TIER1_TRANID",
                confidence= 100,
            )
            matched_pairs.append(pair)
            matched_row_ids.add(row_a["row_id"])
            matched_row_ids.add(row_b["row_id"])

        else:
            # More than 2 rows with same tranid
            # Could be a multi-line AICJE — pass to Tier 2 for hash matching
            print(
                f"[TIER 1] tranid '{tranid_key}' has {len(group)} rows "
                f"across {len(entities_in_group)} entities. "
                f"Passing to Tier 2 for hash matching."
            )

    # Build remaining pool (rows not matched in Tier 1)
    remaining = df[~df["row_id"].isin(matched_row_ids)].copy()

    tier1_count = len(matched_pairs)
    print(
        f"[TIER 1] Complete: {tier1_count} pairs matched. "
        f"{len(remaining)} rows passed to Tier 2."
    )

    if matched_pairs:
        return pd.DataFrame(matched_pairs), remaining
    else:
        return pd.DataFrame(), remaining


# -----------------------------------------------------------------------------
# TIER 2: SHA-256 HASH MATCH
# High confidence — symmetric composite key
# -----------------------------------------------------------------------------

def match_tier2(
    df: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Tier 2 matching: SHA-256 composite hash match for manual JEs.

    Logic:
    - Group rows by match_group_key
    - Detect collisions (buckets where source count != dest count)
    - For symmetric buckets (equal counts): match sequentially by row index
    - For asymmetric buckets (collision): route to manual review exception
    - Remaining unmatched rows pass to Tier 3

    Returns:
        matched_pairs: DataFrame of confirmed Tier 2 matches
        remaining:     DataFrame of rows not matched in Tier 2
    """
    print("\n[TIER 2] Starting SHA-256 hash matching...")

    if len(df) == 0:
        print("[TIER 2] No rows to match. Skipping.")
        return pd.DataFrame(), df

    matched_pairs   = []
    matched_row_ids = set()
    collision_keys  = set()

    # Split into two pools: one per entity perspective
    # Each IC transaction appears twice in the pool (both sides)
    # We group by match_group_key to find candidate pairs

    for key, group in df.groupby("match_group_key"):

        if len(group) < 2:
            # Only one side — orphan candidate for later tiers
            continue

        # Get distinct entity groups within this hash bucket
        entity_groups = group.groupby("local_entity")
        entity_list   = list(entity_groups.groups.keys())

        if len(entity_list) < 2:
            # Both rows from same entity — not a valid IC pair
            continue

        # For exactly 2 entities: attempt pairing
        if len(entity_list) == 2:
            group_a = entity_groups.get_group(entity_list[0])
            group_b = entity_groups.get_group(entity_list[1])

            # Symmetric bucket: equal row counts on both sides
            if len(group_a) == len(group_b):
                # Zip rows sequentially
                # If counts are equal, zip is 1:1 regardless of order
                for row_a, row_b in zip(
                    group_a.itertuples(index=False),
                    group_b.itertuples(index=False),
                ):
                    row_a_series = group_a[
                        group_a["row_id"] == row_a.row_id
                    ].iloc[0]
                    row_b_series = group_b[
                        group_b["row_id"] == row_b.row_id
                    ].iloc[0]

                    pair = _build_match_pair(
                        orig=       row_a_series,
                        recv=       row_b_series,
                        match_tier= "TIER2_HASH",
                        confidence= 90,
                    )
                    matched_pairs.append(pair)
                    matched_row_ids.add(row_a.row_id)
                    matched_row_ids.add(row_b.row_id)

            else:
                # Asymmetric bucket — collision detected
                # Do not auto-match — route entire bucket to Tier 3/4
                collision_keys.add(key)
                print(
                    f"[TIER 2] COLLISION: hash '{key[:12]}...' has "
                    f"{len(group_a)} rows from {entity_list[0]} and "
                    f"{len(group_b)} rows from {entity_list[1]}. "
                    f"Routing to subset sum matching."
                )
        else:
            # More than 2 entities in same hash bucket — unusual
            # Pass to Tier 3 for tolerance matching
            print(
                f"[TIER 2] Multi-entity bucket: hash '{key[:12]}...' "
                f"contains {len(entity_list)} entities. Passing to Tier 3."
            )

    # Build remaining pool
    remaining = df[~df["row_id"].isin(matched_row_ids)].copy()

    tier2_count = len(matched_pairs)
    print(
        f"[TIER 2] Complete: {tier2_count} pairs matched. "
        f"{len(collision_keys)} collision buckets routed forward. "
        f"{len(remaining)} rows passed to Tier 3."
    )

    if matched_pairs:
        return pd.DataFrame(matched_pairs), remaining
    else:
        return pd.DataFrame(), remaining


# -----------------------------------------------------------------------------
# STANDALONE TEST
# Run directly to test Tier 1 and Tier 2 against synthetic data:
#   python src/matching/exact.py
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

    from src.ingestion.ingestor import ingest
    from src.matching.keygen import assign_keys

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

    print("\nAssigning keys...")
    keyed_df = assign_keys(ic_df)

    print("\nRunning Tier 1 matching...")
    t1_pairs, after_t1 = match_tier1(keyed_df)

    print("\nRunning Tier 2 matching...")
    t2_pairs, after_t2 = match_tier2(after_t1)

    print("\n" + "=" * 60)
    print("EXACT MATCHING RESULTS")
    print("=" * 60)
    print(f"Tier 1 matches:  {len(t1_pairs)}")
    print(f"Tier 2 matches:  {len(t2_pairs)}")
    print(f"Remaining rows:  {len(after_t2)}")

    if len(t1_pairs) > 0:
        print("\nTier 1 sample:")
        print(t1_pairs[[
            "orig_entity", "recv_entity",
            "orig_fxamount", "recv_fxamount",
            "confidence_score", "is_fxamount_exact"
        ]].head(5).to_string())

    if len(t2_pairs) > 0:
        print("\nTier 2 sample:")
        print(t2_pairs[[
            "orig_entity", "recv_entity",
            "orig_fxamount", "recv_fxamount",
            "confidence_score", "is_fxamount_exact"
        ]].head(5).to_string())