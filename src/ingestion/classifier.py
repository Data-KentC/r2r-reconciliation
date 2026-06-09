# =============================================================================
# R2R INTERCOMPANY RECONCILIATION ENGINE
# src/ingestion/classifier.py
#
# What this file does:
#   After validation, classifies every GL row into one of three buckets:
#     1. IC transaction (in-scope) — goes to matching engine
#     2. Non-IC transaction — discarded silently
#     3. Out-of-scope entity — quarantined and logged
#
#   IC identification uses three layers in strict priority order:
#     Layer 1: cseg_apac_ic field (primary — 75-85% coverage)
#     Layer 2: Account code range (fallback — catches missing CSEG)
#     Layer 3: Memo field keyword (tertiary — catches wrong account too)
#
# How other files use it:
#   from src.ingestion.classifier import classify
#   ic_df, quarantine_df = classify(validated_df)
# =============================================================================

import re
import pandas as pd
from rapidfuzz import process, fuzz
from typing import Tuple

from src.config import config


# -----------------------------------------------------------------------------
# CONSTANTS
# Derived from config — computed once at module load
# -----------------------------------------------------------------------------

VALID_ENTITIES   = config.valid_entity_codes   # ["ANTH-SG", "ANTH-AU", ...]
NS               = config.netsuite.fields       # Column name aliases

# IC account code ranges from config
IC_FROM_START = int(config.netsuite.ic_due_from_range.start)
IC_FROM_END   = int(config.netsuite.ic_due_from_range.end)
IC_TO_START   = int(config.netsuite.ic_due_to_range.start)
IC_TO_END     = int(config.netsuite.ic_due_to_range.end)

# Memo keyword matching threshold — above this = likely IC transaction
MEMO_IC_THRESHOLD = 85


# -----------------------------------------------------------------------------
# LAYER 1: CSEG IDENTIFICATION
# Primary IC identification — most reliable source
# -----------------------------------------------------------------------------

def _identify_via_cseg(df: pd.DataFrame) -> pd.Series:
    """
    Returns a boolean Series — True where cseg_apac_ic is populated
    AND contains a valid APAC entity code.

    Note: CSEG may be blank on 15-25% of manual JEs.
    Those rows fall through to Layer 2.
    """
    cseg_col = df[NS.cseg_ic].fillna("").str.strip()

    # Check if CSEG value is a known valid entity
    is_valid_cseg = cseg_col.isin(VALID_ENTITIES)

    # Flag rows where CSEG exists but is not a valid entity
    # These may have typos — rapidfuzz correction applied later
    has_cseg_but_invalid = (cseg_col != "") & (~is_valid_cseg)
    if has_cseg_but_invalid.sum() > 0:
        invalid_values = df.loc[has_cseg_but_invalid, NS.cseg_ic].unique()
        print(
            f"[CLASSIFIER] WARNING: {has_cseg_but_invalid.sum()} rows have "
            f"unrecognised CSEG values: {invalid_values}. "
            f"Attempting fuzzy correction."
        )

    return is_valid_cseg


def _fuzzy_correct_cseg(df: pd.DataFrame) -> pd.DataFrame:
    """
    For rows where CSEG is populated but not a valid entity code,
    attempts fuzzy correction using rapidfuzz Levenshtein distance.

    If fuzzy score >= 90: auto-corrects in memory, flags with is_cseg_corrected=True
    If fuzzy score < 90: leaves blank, falls through to Layer 2
    """
    df = df.copy()
    df["is_cseg_corrected"] = False

    cseg_col = df[NS.cseg_ic].fillna("").str.strip()
    has_cseg  = cseg_col != ""
    invalid   = has_cseg & ~cseg_col.isin(VALID_ENTITIES)

    for idx in df[invalid].index:
        raw_value = df.at[idx, NS.cseg_ic]
        result = process.extractOne(
            raw_value,
            VALID_ENTITIES,
            scorer=fuzz.ratio,
        )
        if result and result[1] >= 90:
            corrected = result[0]
            print(
                f"[CLASSIFIER] CSEG auto-corrected: '{raw_value}' → '{corrected}' "
                f"(score: {result[1]}). Row internalid: {df.at[idx, NS.internal_id]}"
            )
            df.at[idx, NS.cseg_ic] = corrected
            df.at[idx, "is_cseg_corrected"] = True
        else:
            # Score too low — clear the CSEG so Layer 2 can try
            df.at[idx, NS.cseg_ic] = ""
            print(
                f"[CLASSIFIER] CSEG too ambiguous to correct: '{raw_value}' "
                f"(best score: {result[1] if result else 0}). "
                f"Falling through to account code check."
            )

    return df


# -----------------------------------------------------------------------------
# LAYER 2: ACCOUNT CODE RANGE IDENTIFICATION
# Fallback for rows with blank or uncorrectable CSEG
# -----------------------------------------------------------------------------

def _extract_account_code(account_string: str) -> int:
    """
    Extracts the numeric prefix from a NetSuite account string.
    NetSuite exports account as "18500 IC Receivable" — we need 18500.

    Returns -1 if no numeric prefix found.
    """
    match = re.match(r"^(\d+)", str(account_string).strip())
    return int(match.group(1)) if match else -1


def _identify_via_account_code(df: pd.DataFrame) -> pd.Series:
    """
    Returns a boolean Series — True where the account code falls within
    the IC Due From or IC Due To ranges defined in config.yaml.

    Used only for rows where CSEG is blank after Layer 1.
    """
    account_codes = df[NS.account].apply(_extract_account_code)

    in_due_from = (account_codes >= IC_FROM_START) & (account_codes <= IC_FROM_END)
    in_due_to   = (account_codes >= IC_TO_START)   & (account_codes <= IC_TO_END)

    return in_due_from | in_due_to


def _derive_counterparty_from_account(account_string: str) -> str:
    """
    Attempts to derive the IC counterparty from the account code suffix.
    Example: "18002 Due From ANTH-AU" → "ANTH-AU"

    Returns empty string if no entity found in account name.
    """
    for entity in VALID_ENTITIES:
        if entity in str(account_string):
            return entity
    return ""


# -----------------------------------------------------------------------------
# LAYER 3: MEMO FIELD HEURISTIC
# Tertiary identification for rows that failed both CSEG and account code
# Uses rapidfuzz partial_ratio to detect entity names in memo text
# -----------------------------------------------------------------------------

def _identify_via_memo(df: pd.DataFrame) -> Tuple[pd.Series, pd.Series]:
    """
    Scans memo field for entity name mentions using rapidfuzz partial_ratio.
    Returns two Series:
        is_ic_via_memo: bool — True if memo suggests IC transaction
        suggested_entity: str — the entity detected in the memo
    """
    is_ic_via_memo   = pd.Series(False,  index=df.index)
    suggested_entity = pd.Series("",     index=df.index)

    memo_col = df[NS.memo].fillna("").astype(str)

    for idx, memo in memo_col.items():
        if not memo.strip():
            continue

        result = process.extractOne(
            memo,
            VALID_ENTITIES,
            scorer=fuzz.partial_ratio,
        )

        if result and result[1] >= MEMO_IC_THRESHOLD:
            is_ic_via_memo.at[idx]   = True
            suggested_entity.at[idx] = result[0]

    return is_ic_via_memo, suggested_entity


# -----------------------------------------------------------------------------
# OUT-OF-SCOPE FILTER
# Removes rows from entities outside the APAC 5
# These are quarantined separately — not discarded silently
# -----------------------------------------------------------------------------

def _filter_out_of_scope(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Splits DataFrame into in-scope (APAC 5) and out-of-scope rows.

    Out-of-scope rows are caused by NetSuite Saved Search parameter leakage —
    e.g., ANTH-US appearing in an APAC-only export.

    Returns:
        in_scope_df:   Rows where subsidiary is in VALID_ENTITIES
        quarantine_df: Rows where subsidiary is outside VALID_ENTITIES
    """
    subsidiary_col = df[NS.line_subsidiary].fillna("").str.strip()

    in_scope_mask  = subsidiary_col.isin(VALID_ENTITIES)
    out_scope_mask = ~in_scope_mask

    out_of_scope_count = out_scope_mask.sum()
    if out_of_scope_count > 0:
        out_entities = df.loc[out_scope_mask, NS.line_subsidiary].unique()
        print(
            f"[CLASSIFIER] OUT-OF-SCOPE: {out_of_scope_count} rows from "
            f"non-APAC entities quarantined: {out_entities}. "
            f"These will not be processed by the matching engine."
        )

    return df[in_scope_mask].copy(), df[out_scope_mask].copy()


# -----------------------------------------------------------------------------
# REVERSAL PRE-FILTER
# Drops entries that net to zero within the same internalid + account
# These are fully reversed transactions — no IC balance impact
# -----------------------------------------------------------------------------

def _filter_reversals(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Identifies and removes fully reversed transactions.

    Logic:
    - Group by internalid + account + cseg_apac_ic
    - Calculate net fxamount (debit positive, credit negative)
    - If net == 0: fully reversed → drop from matching pool
    - If net != 0: partial reversal → collapse to net amount, flag

    Returns:
        clean_df:    Rows with non-zero net (ready for matching)
        reversal_df: Rows that netted to zero (logged, not matched)
    """
    df = df.copy()

    # Calculate signed fxamount: debits positive, credits negative
    debit  = df[NS.debit_amount].fillna(0)
    credit = df[NS.credit_amount].fillna(0)
    df["net_amount"] = debit - credit

    # Group and sum net amounts
    group_cols = [NS.internal_id, NS.account, NS.cseg_ic]
    net_by_group = df.groupby(group_cols)["net_amount"].transform("sum")
    df["group_net"] = net_by_group

    # Full reversals: group net == 0
    full_reversal_mask    = df["group_net"].abs() < 0.001  # Float tolerance
    partial_reversal_mask = (~full_reversal_mask) & (
        df.groupby(group_cols)[NS.internal_id].transform("count") > 1
    )

    full_reversal_count = full_reversal_mask.sum()
    if full_reversal_count > 0:
        print(
            f"[CLASSIFIER] REVERSALS: {full_reversal_count} rows dropped — "
            f"they net to zero within the same transaction group. "
            f"No IC balance impact."
        )

    # Flag partial reversals
    df["is_partial_reversal"] = partial_reversal_mask

    clean_df    = df[~full_reversal_mask].copy()
    reversal_df = df[full_reversal_mask].copy()

    return clean_df, reversal_df


# -----------------------------------------------------------------------------
# MAIN CLASSIFY FUNCTION
# Called by ingestor.py — single entry point into this module
# -----------------------------------------------------------------------------

def classify(validated_df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Classifies all validated GL rows into IC and non-IC buckets.

    Processing order:
    1. Filter out-of-scope entities → quarantine
    2. Apply reversal pre-filter → drop zero-net groups
    3. Layer 1: CSEG identification (with fuzzy correction)
    4. Layer 2: Account code range (fallback for blank CSEG)
    5. Layer 3: Memo heuristic (tertiary fallback)
    6. Mark identification source on each row

    Returns:
        ic_df:        IC transactions ready for matching engine
        quarantine_df: Out-of-scope rows for logging only
    """
    print(f"\n[CLASSIFIER] Starting classification. Input: {len(validated_df)} rows")

    # Step 1: Filter out-of-scope entities
    in_scope_df, quarantine_df = _filter_out_of_scope(validated_df)
    print(f"[CLASSIFIER] After scope filter: {len(in_scope_df)} in-scope rows")

    if len(in_scope_df) == 0:
        print("[CLASSIFIER] WARNING: No in-scope rows after entity filter.")
        return pd.DataFrame(), quarantine_df

    # Step 2: Apply reversal pre-filter
    in_scope_df, reversal_df = _filter_reversals(in_scope_df)
    print(
        f"[CLASSIFIER] After reversal filter: {len(in_scope_df)} rows "
        f"({len(reversal_df)} reversals dropped)"
    )

    # Step 3: Fuzzy-correct invalid CSEG values before identification
    in_scope_df = _fuzzy_correct_cseg(in_scope_df)

    # Initialise identification columns
    in_scope_df["is_ic"]              = False
    in_scope_df["ic_source"]          = ""   # CSEG | ACCOUNT_CODE | MEMO | NONE
    in_scope_df["ic_counterparty"]    = ""   # Derived counterparty entity
    in_scope_df["is_partial_reversal"] = in_scope_df.get(
        "is_partial_reversal", False
    )

    # Step 4: Layer 1 — CSEG identification
    cseg_mask = _identify_via_cseg(in_scope_df)
    in_scope_df.loc[cseg_mask, "is_ic"]           = True
    in_scope_df.loc[cseg_mask, "ic_source"]       = "CSEG"
    in_scope_df.loc[cseg_mask, "ic_counterparty"] = (
        in_scope_df.loc[cseg_mask, NS.cseg_ic]
    )

    layer1_count = cseg_mask.sum()
    print(f"[CLASSIFIER] Layer 1 (CSEG): {layer1_count} IC rows identified")

    # Step 5: Layer 2 — Account code range (rows not yet identified)
    unidentified = ~in_scope_df["is_ic"]
    account_mask = _identify_via_account_code(in_scope_df) & unidentified

    in_scope_df.loc[account_mask, "is_ic"]     = True
    in_scope_df.loc[account_mask, "ic_source"] = "ACCOUNT_CODE"

    # Try to derive counterparty from account name
    for idx in in_scope_df[account_mask].index:
        account_str = in_scope_df.at[idx, NS.account]
        counterparty = _derive_counterparty_from_account(account_str)
        in_scope_df.at[idx, "ic_counterparty"] = counterparty

    layer2_count = account_mask.sum()
    print(f"[CLASSIFIER] Layer 2 (Account): {layer2_count} IC rows identified")

    # Step 6: Layer 3 — Memo heuristic (rows still not identified)
    unidentified = ~in_scope_df["is_ic"]
    if unidentified.sum() > 0:
        is_ic_memo, suggested = _identify_via_memo(
            in_scope_df[unidentified]
        )
        memo_ic_idx = is_ic_memo[is_ic_memo].index

        in_scope_df.loc[memo_ic_idx, "is_ic"]           = True
        in_scope_df.loc[memo_ic_idx, "ic_source"]       = "MEMO"
        in_scope_df.loc[memo_ic_idx, "ic_counterparty"] = suggested[memo_ic_idx]

        layer3_count = len(memo_ic_idx)
        print(
            f"[CLASSIFIER] Layer 3 (Memo): {layer3_count} IC rows identified. "
            f"Note: memo-based identification requires human review."
        )
    else:
        layer3_count = 0
        print("[CLASSIFIER] Layer 3 (Memo): skipped — all rows already identified")

    # Summary
    ic_df     = in_scope_df[in_scope_df["is_ic"]].copy()
    non_ic_df = in_scope_df[~in_scope_df["is_ic"]].copy()

    print(
        f"\n[CLASSIFIER] Classification complete:"
        f"\n  IC transactions:     {len(ic_df)}"
        f"\n  Non-IC discarded:    {len(non_ic_df)}"
        f"\n  Out-of-scope:        {len(quarantine_df)}"
        f"\n  Reversals dropped:   {len(reversal_df)}"
        f"\n  Total input rows:    {len(validated_df)}"
    )

    # Warn if memo-based IC rows exist — these need human review
    memo_rows = ic_df[ic_df["ic_source"] == "MEMO"]
    if len(memo_rows) > 0:
        print(
            f"\n[CLASSIFIER] REVIEW REQUIRED: {len(memo_rows)} rows were "
            f"identified as IC only via memo text. These will appear in "
            f"exceptions with ic_source=MEMO for controller review."
        )

    return ic_df, quarantine_df