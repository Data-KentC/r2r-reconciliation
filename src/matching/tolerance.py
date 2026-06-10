# =============================================================================
# R2R INTERCOMPANY RECONCILIATION ENGINE
# src/matching/tolerance.py
#
# What this file does:
#   Implements Tier 3 matching — tolerance-based matching for transactions
#   that failed exact matching but are likely the same economic event.
#
#   Two types of tolerance are applied:
#
#   Type A — FX Tolerance:
#     Two transactions match if the variance in fxamount is:
#       ≤ $100 USD absolute  AND  ≤ 1% of the larger amount
#     This handles FX rounding noise from different exchange rate feeds.
#     Variance is decomposed into:
#       fx_rounding_variance:  attributable to rate differential
#       operational_variance:  the residual — a real accounting dispute
#
#   Type B — Timing Gap:
#     Two transactions match if they are for the same amount and currency
#     but posted in different periods (±1 period allowed).
#     These are classified as TIMING_GAP — goods in transit, invoice delays.
#     A JE Draft is generated to create an in-transit accrual.
#
#   Fuzzy account name matching (rapidfuzz) is also applied here to handle
#   cases where both sides used slightly different account descriptions.
#
# How other files use it:
#   from src.matching.tolerance import match_tier3
#   matched_pairs, remaining = match_tier3(remaining_df)
# =============================================================================

from typing import Tuple, List
from datetime import datetime

import pandas as pd
from rapidfuzz import fuzz, distance

from src.config import config
from src.matching.exact import _build_match_pair


# -----------------------------------------------------------------------------
# CONSTANTS
# -----------------------------------------------------------------------------

NS          = config.netsuite.fields
MAT         = config.materiality
TIER3       = config.matching.tier_3
FUZZY_CFG   = config.matching.fuzzy


# -----------------------------------------------------------------------------
# FX VARIANCE CALCULATOR
# Decomposes total variance into FX rounding and operational components
# -----------------------------------------------------------------------------

def _decompose_variance(
    orig_fxamount: float,
    recv_fxamount: float,
) -> Tuple[float, float, float]:
    """
    Decomposes the total variance between two fxamount values.

    The key insight:
        If both entities agree on the FOREIGN CURRENCY AMOUNT (fxamount)
        but differ on base currency translation → FX rounding variance.
        If they disagree on the foreign currency amount itself → operational.

    For our matching purposes:
        We match on fxamount (foreign currency).
        Any difference in fxamount = the variance we are measuring.

    Decomposition:
        total_variance      = abs(orig_fxamount - recv_fxamount)
        fx_rounding         = min(total_variance, materiality threshold)
        operational         = total_variance - fx_rounding

    This is a simplified decomposition suitable for prototype.
    Production V2: use actual exchange rate differentials per transaction.

    Returns:
        (total_variance, fx_rounding_variance, operational_variance)
    """
    total = abs(orig_fxamount - recv_fxamount)

    # If variance is entirely within FX rounding threshold, all is FX noise
    if total <= MAT.fx_rounding_threshold:
        return total, total, 0.0

    # Otherwise split: FX threshold is noise, remainder is operational
    fx_rounding  = MAT.fx_rounding_threshold
    operational  = total - fx_rounding

    return total, fx_rounding, operational


# -----------------------------------------------------------------------------
# TOLERANCE CHECKER
# Applies the dual-condition materiality threshold
# -----------------------------------------------------------------------------

def _is_within_tolerance(
    orig_fxamount: float,
    recv_fxamount: float,
) -> Tuple[bool, str]:
    """
    Checks if the variance between two fxamount values is within tolerance.

    Dual condition (BOTH must be true):
        Absolute: abs(orig - recv) ≤ config.materiality.variance_threshold_usd
        Relative: abs(orig - recv) / max(orig, recv) ≤ variance_threshold_pct

    Returns:
        (within_tolerance: bool, reason: str)
        reason values: "EXACT" | "FX_ROUNDING" | "EXCEEDS_THRESHOLD"
    """
    if orig_fxamount == 0 and recv_fxamount == 0:
        return True, "EXACT"

    variance = abs(orig_fxamount - recv_fxamount)

    if variance == 0:
        return True, "EXACT"

    # Absolute threshold check
    abs_ok = variance <= MAT.variance_threshold_usd

    # Percentage threshold check
    larger_amount = max(abs(orig_fxamount), abs(recv_fxamount))
    pct_variance  = variance / larger_amount if larger_amount > 0 else 1.0
    pct_ok        = pct_variance <= MAT.variance_threshold_pct

    if abs_ok and pct_ok:
        return True, "FX_ROUNDING"

    return False, "EXCEEDS_THRESHOLD"


# -----------------------------------------------------------------------------
# TIMING GAP CHECKER
# Checks if two dates are in adjacent periods (±1 month)
# -----------------------------------------------------------------------------

def _get_period_delta(date_a: str, date_b: str) -> int:
    """
    Calculates the absolute difference in months between two date strings.

    Examples:
        "2026-06-29" vs "2026-07-04" → 1  (timing gap, in scope)
        "2026-06-01" vs "2026-08-01" → 2  (too old, out of scope)
        "2026-06-15" vs "2026-06-20" → 0  (same period)

    Returns:
        Integer month difference (0, 1, 2, ...)
    """
    try:
        dt_a = datetime.strptime(str(date_a).strip()[:10], "%Y-%m-%d")
        dt_b = datetime.strptime(str(date_b).strip()[:10], "%Y-%m-%d")
        year_diff  = abs(dt_a.year  - dt_b.year)
        month_diff = abs(dt_a.month - dt_b.month)
        return year_diff * 12 + month_diff
    except Exception:
        return 99  # Unparseable date — treat as out of scope


def _is_timing_gap(date_a: str, date_b: str) -> Tuple[bool, int]:
    """
    Checks if two transaction dates span a period boundary.

    Returns:
        (is_timing_gap: bool, period_delta: int)
    """
    delta = _get_period_delta(date_a, date_b)
    max_gap = TIER3.timing_gap_periods  # 1 from config.yaml

    if delta == 0:
        return False, 0          # Same period — not a timing gap
    elif delta <= max_gap:
        return True, delta        # Adjacent period — timing gap, in scope
    else:
        return False, delta       # Too far apart — out of scope


# -----------------------------------------------------------------------------
# FUZZY ACCOUNT NAME MATCHER
# Composite score from Levenshtein + Jaro-Winkler + N-gram
# -----------------------------------------------------------------------------

def _fuzzy_account_score(account_a: str, account_b: str) -> float:
    """
    Computes a composite fuzzy similarity score between two account names.

    Three algorithms combined with configurable weights:
        Levenshtein token_set_ratio: handles word reordering
        Jaro-Winkler:                weights prefix matches (good for codes)
        N-gram (bigram):             handles partial word overlaps

    Returns:
        Float 0.0-100.0 (100 = identical)
    """
    a = str(account_a).strip()
    b = str(account_b).strip()

    if not a or not b:
        return 0.0

    # Levenshtein token set ratio — best for reordered words
    lev_score = fuzz.token_set_ratio(a, b)

    # Jaro-Winkler — best for prefix-similar strings
    jw_score  = distance.JaroWinkler.similarity(a, b) * 100

    # N-gram bigram similarity
    def ngram_sim(s1: str, s2: str, n: int = 2) -> float:
        g1 = set(s1[i:i+n] for i in range(len(s1) - n + 1))
        g2 = set(s2[i:i+n] for i in range(len(s2) - n + 1))
        if not g1 or not g2:
            return 0.0
        return len(g1 & g2) / len(g1 | g2) * 100

    ngram_score = ngram_sim(a.lower(), b.lower())

    # Weighted composite
    composite = (
        FUZZY_CFG.levenshtein_weight  * lev_score +
        FUZZY_CFG.jaro_winkler_weight * jw_score  +
        FUZZY_CFG.ngram_weight        * ngram_score
    )

    return round(composite, 1)


# -----------------------------------------------------------------------------
# CANDIDATE FINDER
# For each unmatched row, finds the best candidate on the other side
# -----------------------------------------------------------------------------

def _find_best_candidate(
    row:        pd.Series,
    pool:       pd.DataFrame,
) -> Tuple[pd.Series, float, str, bool, int]:
    """
    Finds the best matching candidate for a given row from the opposing pool.

    Matching criteria (in priority order):
    1. Currency must match (hard requirement — no cross-currency matching)
    2. Entities must be correctly paired (row's counterparty = candidate's entity)
    3. fxamount within tolerance (dual-condition)
    4. Fuzzy account name similarity above threshold
    5. Timing gap within ±1 period if fxamount matches

    Returns:
        (best_candidate, fuzzy_score, tolerance_reason, is_timing_gap, period_delta)
        Returns (None, 0, "", False, 0) if no candidate found.
    """
    # Filter pool to candidates from the expected counterparty entity
    expected_counterparty = str(row["ic_counterparty"]).strip()
    currency              = str(row[NS.currency]).strip()
    row_fxamount          = float(row[NS.fx_amount]) if row[NS.fx_amount] else 0.0

    # Hard filter: currency match + correct entity pair
    candidates = pool[
        (pool[NS.currency] == currency) &
        (pool["local_entity"] == expected_counterparty) &
        (pool["ic_counterparty"] == row["local_entity"])
    ]

    if len(candidates) == 0:
        return None, 0.0, "", False, 0

    best_candidate   = None
    best_score       = 0.0
    best_reason      = ""
    best_timing_gap  = False
    best_period_delta = 0

    for _, candidate in candidates.iterrows():
        cand_fxamount = float(candidate[NS.fx_amount]) if candidate[NS.fx_amount] else 0.0

        # Check tolerance
        within_tol, reason = _is_within_tolerance(row_fxamount, cand_fxamount)
        if not within_tol:
            continue

        # Check timing gap
        timing_gap, period_delta = _is_timing_gap(
            row[NS.tran_date],
            candidate[NS.tran_date],
        )

        # Fuzzy account name score
        fuzzy_score = _fuzzy_account_score(
            row["account_name"],
            candidate["account_name"],
        )

        # Combined score (tolerance match already confirmed above)
        combined = fuzzy_score
        if combined > best_score:
            best_score        = combined
            best_candidate    = candidate
            best_reason       = reason
            best_timing_gap   = timing_gap
            best_period_delta = period_delta

    return best_candidate, best_score, best_reason, best_timing_gap, best_period_delta


# -----------------------------------------------------------------------------
# MAIN TIER 3 MATCHING FUNCTION
# -----------------------------------------------------------------------------

def match_tier3(
    df: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Tier 3 matching: tolerance-based matching with FX and timing gap support.

    Processes unmatched rows from Tier 1 and Tier 2.

    For each unmatched row:
    1. Find candidates from the opposing entity within tolerance
    2. If found: match and classify as FX_ROUNDING or TIMING_GAP
    3. If not found: pass to Tier 4 (subset sum)

    Returns:
        matched_pairs: DataFrame of Tier 3 matches
        remaining:     DataFrame of rows not matched in Tier 3
    """
    print("\n[TIER 3] Starting tolerance matching...")

    if len(df) == 0:
        print("[TIER 3] No rows to match. Skipping.")
        return pd.DataFrame(), df

    matched_pairs   = []
    matched_row_ids = set()

    # Process each row — look for a matching candidate in the pool
    for idx, row in df.iterrows():

        if row["row_id"] in matched_row_ids:
            continue  # Already matched in this pass

        best, fuzzy_score, reason, timing_gap, period_delta = (
            _find_best_candidate(
                row=  row,
                pool= df[~df["row_id"].isin(matched_row_ids)],
            )
        )

        if best is None:
            continue  # No candidate found — pass to Tier 4

        if fuzzy_score < FUZZY_CFG.levenshtein_threshold:
            # Account names too different — pass to Tier 4
            continue

        # Determine tolerance reason
        if timing_gap:
            tolerance_reason = "TIMING_GAP"
        elif reason == "FX_ROUNDING":
            tolerance_reason = "FX_ROUNDING"
        else:
            tolerance_reason = "TOLERANCE"

        # Compute variance decomposition
        orig_fx = float(row[NS.fx_amount]) if row[NS.fx_amount] else 0.0
        recv_fx = float(best[NS.fx_amount]) if best[NS.fx_amount] else 0.0
        total_var, fx_var, op_var = _decompose_variance(orig_fx, recv_fx)

        # Build the match pair
        pair = _build_match_pair(
            orig=       row,
            recv=       best,
            match_tier= "TIER3_TOLERANCE",
            confidence= 75,
        )

        # Override tolerance fields
        pair["tolerance_reason"]        = tolerance_reason
        pair["is_period_matched"]       = (period_delta == 0)
        pair["variance_fxamount"]       = total_var
        pair["variance_usd"]            = total_var  # Simplified for prototype
        pair["fx_rounding_variance"]    = fx_var
        pair["operational_variance"]    = op_var
        pair["fuzzy_account_score"]     = fuzzy_score
        pair["period_delta_months"]     = period_delta

        # Adjust confidence for timing gaps
        if timing_gap:
            pair["confidence_score"] = max(0, pair["confidence_score"] - 10)

        matched_pairs.append(pair)
        matched_row_ids.add(row["row_id"])
        matched_row_ids.add(best["row_id"])

    # Build remaining pool
    remaining = df[~df["row_id"].isin(matched_row_ids)].copy()

    # Summary
    fx_matches     = sum(1 for p in matched_pairs if p["tolerance_reason"] == "FX_ROUNDING")
    timing_matches = sum(1 for p in matched_pairs if p["tolerance_reason"] == "TIMING_GAP")

    print(
        f"[TIER 3] Complete: {len(matched_pairs)} pairs matched "
        f"({fx_matches} FX rounding, {timing_matches} timing gaps). "
        f"{len(remaining)} rows passed to Tier 4."
    )

    if matched_pairs:
        return pd.DataFrame(matched_pairs), remaining
    else:
        return pd.DataFrame(), remaining


# -----------------------------------------------------------------------------
# STANDALONE TEST
# Run directly to test Tier 3 against synthetic data:
#   python src/matching/tolerance.py
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

    from src.ingestion.ingestor import ingest
    from src.matching.keygen   import assign_keys
    from src.matching.exact    import match_tier1, match_tier2

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

    ic_df, meta  = ingest(synthetic_path)
    keyed_df     = assign_keys(ic_df)
    _, after_t1  = match_tier1(keyed_df)
    _, after_t2  = match_tier2(after_t1)

    print("\nRunning Tier 3 tolerance matching...")
    t3_pairs, after_t3 = match_tier3(after_t2)

    print("\n" + "=" * 60)
    print("TIER 3 RESULTS")
    print("=" * 60)
    print(f"Tier 3 matches:  {len(t3_pairs)}")
    print(f"Remaining rows:  {len(after_t3)}")

    if len(t3_pairs) > 0:
        print("\nTier 3 sample:")
        print(t3_pairs[[
            "orig_entity", "recv_entity",
            "orig_fxamount", "recv_fxamount",
            "variance_fxamount", "tolerance_reason",
            "confidence_score",
        ]].to_string())