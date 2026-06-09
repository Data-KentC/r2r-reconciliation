# =============================================================================
# R2R INTERCOMPANY RECONCILIATION ENGINE
# src/ingestion/ingestor.py
#
# What this file does:
#   Orchestrates the full ingestion pipeline for a NetSuite GL CSV export.
#   Takes a raw CSV file path and returns a clean, classified DataFrame
#   ready for the matching engine.
#
#   Processing sequence:
#     1. Read CSV with correct encoding and dtype settings
#     2. Validate schema (Pandera — via validator.py)
#     3. Compute net_amount per row (debit - credit)
#     4. Parse account code and account name from concatenated string
#     5. Classify rows: IC vs non-IC vs out-of-scope (via classifier.py)
#     6. Compute MD5 hash of input file for audit chain of custody
#     7. Return clean IC DataFrame + metadata
#
# How other files use it:
#   from src.ingestion.ingestor import ingest
#   ic_df, meta = ingest("data/real/Jun2026_GL.csv")
#
# Output DataFrame columns (in addition to original NetSuite columns):
#   net_amount          float   debit - credit (signed)
#   account_code        str     parsed from account string e.g. "18500"
#   account_name        str     parsed from account string e.g. "IC Receivable"
#   local_entity        str     same as subsidiarynohierarchy (alias)
#   is_ic               bool    True if identified as IC transaction
#   ic_source           str     CSEG | ACCOUNT_CODE | MEMO
#   ic_counterparty     str     derived counterparty entity code
#   is_fx_coalesced     bool    True if fxamount was null and coalesced
#   is_cseg_corrected   bool    True if CSEG was fuzzy-corrected
#   is_partial_reversal bool    True if row is part of a partial reversal
#   net_amount          float   signed net fxamount for matching
# =============================================================================

import hashlib
import os
import re
import sys
from datetime import datetime
from dataclasses import dataclass
from typing import Tuple, Optional

import pandas as pd

from src.config import config
from src.ingestion.validator import validate_csv
from src.ingestion.classifier import classify


# -----------------------------------------------------------------------------
# INGESTION METADATA
# Returned alongside the DataFrame so the pipeline has full context
# -----------------------------------------------------------------------------

@dataclass
class IngestionMeta:
    """
    Metadata about a completed ingestion run.
    Stored in the run_log table and embedded in the Excel output header.
    """
    file_path:         str
    file_md5:          str       # MD5 hash of input file — audit chain of custody
    ingested_at:       datetime
    total_rows_raw:    int       # Rows in the raw CSV
    total_rows_ic:     int       # IC rows passed to matching engine
    rows_non_ic:       int       # Non-IC rows discarded
    rows_out_of_scope: int       # Out-of-scope entity rows quarantined
    rows_reversed:     int       # Rows dropped by reversal pre-filter
    rows_fx_coalesced: int       # Rows where fxamount was null and coalesced
    rows_cseg_corrected: int     # Rows where CSEG was fuzzy-corrected
    rows_memo_identified: int    # Rows identified via memo (need review)
    period:            str       # Posting period e.g. "Jun 2026"
    encoding_used:     str       # utf-8 | utf-8-sig


# -----------------------------------------------------------------------------
# FILE UTILITIES
# -----------------------------------------------------------------------------

def _compute_md5(file_path: str) -> str:
    """
    Computes MD5 hash of the input CSV file.
    Used to prove the exact file that was processed — SOX chain of custody.

    Two runs on the same file will always produce the same hash.
    Any modification to the file produces a different hash.
    """
    hasher = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _detect_encoding(file_path: str) -> str:
    """
    Detects whether the CSV has a BOM (Byte Order Mark).
    A BOM appears when a file is opened and saved in Excel before processing.

    Returns:
        "utf-8-sig" if BOM detected (Excel-saved file)
        "utf-8"     if no BOM (direct NetSuite export — correct)
    """
    with open(file_path, "rb") as f:
        raw_start = f.read(3)
    if raw_start.startswith(b"\xef\xbb\xbf"):
        print(
            "[INGESTOR] WARNING: BOM detected in CSV file. "
            "This file was likely opened and saved in Excel before processing. "
            "Using utf-8-sig encoding. "
            "For best results, export directly from NetSuite without opening in Excel."
        )
        return "utf-8-sig"
    return "utf-8"


# -----------------------------------------------------------------------------
# ACCOUNT STRING PARSER
# NetSuite exports account as "18500 IC Receivable" — we need both parts
# -----------------------------------------------------------------------------

def _parse_account(account_string: str) -> Tuple[str, str]:
    """
    Parses a NetSuite concatenated account string into code and name.

    Examples:
        "18500 IC Receivable"        → ("18500", "IC Receivable")
        "6010 Office Supplies"        → ("6010", "Office Supplies")
        "28001 Due To ANTH-SG"        → ("28001", "Due To ANTH-SG")
        "InvalidString"               → ("", "InvalidString")

    Returns:
        (account_code, account_name) as strings
    """
    account_string = str(account_string).strip()
    match = re.match(r"^(\d+)\s+(.+)$", account_string)
    if match:
        return match.group(1), match.group(2).strip()
    return "", account_string


# -----------------------------------------------------------------------------
# NET AMOUNT CALCULATOR
# Computes signed net amount from mutually exclusive debit/credit columns
# -----------------------------------------------------------------------------

def _compute_net_amount(df: pd.DataFrame) -> pd.Series:
    """
    Computes signed net amount from debit and credit columns.

    NetSuite debit/credit columns are mutually exclusive per row:
    - Debit rows:  debitamount has value, creditamount is null
    - Credit rows: creditamount has value, debitamount is null

    Net amount convention:
    - Debits  → positive values
    - Credits → negative values

    This matches standard accounting sign convention for IC matching.
    """
    ns = config.netsuite.fields
    debit  = df[ns.debit_amount].fillna(0)
    credit = df[ns.credit_amount].fillna(0)
    return debit - credit


# -----------------------------------------------------------------------------
# PERIOD EXTRACTOR
# Identifies the dominant posting period in the dataset
# -----------------------------------------------------------------------------

def _extract_period(df: pd.DataFrame) -> str:
    """
    Identifies the most common posting period in the dataset.

    NetSuite exports Posting Period as "Jun 2026" plain string.
    We preserve this as-is and identify the dominant period for
    run metadata. Multi-period exports (e.g. timing gap CSVs) will
    show the most frequent period.
    """
    ns = config.netsuite.fields

    # Try to derive period from trandate (more reliable than a period column)
    try:
        dates = pd.to_datetime(df[ns.tran_date], errors="coerce")
        most_common_date = dates.mode()[0]
        return most_common_date.strftime("%b %Y")  # "Jun 2026"
    except Exception:
        return "Unknown"


# -----------------------------------------------------------------------------
# MAIN INGEST FUNCTION
# Single entry point — called by run_pipeline.py
# -----------------------------------------------------------------------------

def ingest(file_path: str) -> Tuple[pd.DataFrame, IngestionMeta]:
    """
    Full ingestion pipeline for a NetSuite GL CSV export.

    Args:
        file_path: Path to the raw NetSuite CSV file.

    Returns:
        ic_df:  Clean IC transactions DataFrame ready for matching engine.
        meta:   IngestionMeta with audit trail information.

    Raises:
        FileNotFoundError: If the CSV file does not exist.
        SystemExit:        If schema validation fails (via validator.py).
    """
    print("\n" + "=" * 60)
    print("R2R INGESTION PIPELINE")
    print("=" * 60)

    # ------------------------------------------------------------------
    # Step 0: File existence check
    # ------------------------------------------------------------------
    if not os.path.exists(file_path):
        raise FileNotFoundError(
            f"CSV file not found: {file_path}. "
            f"Ensure the NetSuite export was downloaded before running."
        )

    # ------------------------------------------------------------------
    # Step 1: Compute MD5 for audit chain of custody
    # ------------------------------------------------------------------
    file_md5 = _compute_md5(file_path)
    print(f"[INGESTOR] File:     {file_path}")
    print(f"[INGESTOR] MD5:      {file_md5}")

    # ------------------------------------------------------------------
    # Step 2: Detect encoding
    # ------------------------------------------------------------------
    encoding = _detect_encoding(file_path)
    print(f"[INGESTOR] Encoding: {encoding}")

    # ------------------------------------------------------------------
    # Step 3: Read CSV
    # All columns read as string initially — validator handles type conversion
    # This prevents pandas from silently mangling dates, amounts, or IDs
    # ------------------------------------------------------------------
    ns = config.netsuite.fields

    raw_df = pd.read_csv(
        file_path,
        dtype=str,              # Read everything as string first
        encoding=encoding,
        keep_default_na=False,  # Don't auto-convert empty strings to NaN
        na_values=[""],         # Only treat empty string as NaN
    )

    total_rows_raw = len(raw_df)
    print(f"[INGESTOR] Rows read: {total_rows_raw}")
    print(f"[INGESTOR] Columns:   {list(raw_df.columns)}")

    # ------------------------------------------------------------------
    # Step 4: Schema validation (Pandera)
    # Stops pipeline with plain English error if CSV structure is wrong
    # ------------------------------------------------------------------
    print("\n[INGESTOR] Running schema validation...")
    validated_df = validate_csv(raw_df)

    # ------------------------------------------------------------------
    # Step 5: Parse account code and name
    # "18500 IC Receivable" → account_code="18500", account_name="IC Receivable"
    # ------------------------------------------------------------------
    parsed = validated_df[ns.account].apply(_parse_account)
    validated_df["account_code"] = parsed.apply(lambda x: x[0])
    validated_df["account_name"] = parsed.apply(lambda x: x[1])
    print(f"[INGESTOR] Account codes parsed.")

    # ------------------------------------------------------------------
    # Step 6: Compute net amount
    # debit - credit → signed net amount per row
    # ------------------------------------------------------------------
    validated_df["net_amount"] = _compute_net_amount(validated_df)

    # ------------------------------------------------------------------
    # Step 7: Add local_entity alias
    # subsidiarynohierarchy → local_entity (cleaner name for downstream use)
    # ------------------------------------------------------------------
    validated_df["local_entity"] = (
        validated_df[ns.line_subsidiary].fillna("").str.strip()
    )

    # ------------------------------------------------------------------
    # Step 8: Classify rows (IC identification + out-of-scope filter)
    # ------------------------------------------------------------------
    print("\n[INGESTOR] Running classification...")
    ic_df, quarantine_df = classify(validated_df)

    # ------------------------------------------------------------------
    # Step 9: Extract dominant period for metadata
    # ------------------------------------------------------------------
    period = _extract_period(ic_df) if len(ic_df) > 0 else "Unknown"

    # ------------------------------------------------------------------
    # Step 10: Build metadata
    # ------------------------------------------------------------------
    meta = IngestionMeta(
        file_path=            file_path,
        file_md5=             file_md5,
        ingested_at=          datetime.utcnow(),
        total_rows_raw=       total_rows_raw,
        total_rows_ic=        len(ic_df),
        rows_non_ic=          len(validated_df) - len(ic_df) - len(quarantine_df),
        rows_out_of_scope=    len(quarantine_df),
        rows_reversed=        total_rows_raw - len(validated_df),
        rows_fx_coalesced=    int(ic_df.get("is_fx_coalesced",
                                  pd.Series([False])).sum()),
        rows_cseg_corrected=  int(ic_df.get("is_cseg_corrected",
                                  pd.Series([False])).sum()),
        rows_memo_identified= int(
                                  (ic_df.get("ic_source", pd.Series([""]))
                                   == "MEMO").sum()
                              ),
        period=               period,
        encoding_used=        encoding,
    )

    # ------------------------------------------------------------------
    # Step 11: Final summary
    # ------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("INGESTION COMPLETE")
    print("=" * 60)
    print(f"  Period detected:      {meta.period}")
    print(f"  Raw rows:             {meta.total_rows_raw}")
    print(f"  IC rows for matching: {meta.total_rows_ic}")
    print(f"  Non-IC discarded:     {meta.rows_non_ic}")
    print(f"  Out-of-scope:         {meta.rows_out_of_scope}")
    print(f"  FX coalesced:         {meta.rows_fx_coalesced}")
    print(f"  CSEG corrected:       {meta.rows_cseg_corrected}")
    print(f"  Memo-identified:      {meta.rows_memo_identified} (review needed)")
    print(f"  MD5:                  {meta.file_md5}")
    print("=" * 60 + "\n")

    return ic_df, meta


# -----------------------------------------------------------------------------
# STANDALONE TEST
# Run directly to test ingestion against synthetic data:
#   python src/ingestion/ingestor.py
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

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

    ic_df, meta = ingest(synthetic_path)

    print("\nSAMPLE OF IC ROWS:")
    print(ic_df[[
        "local_entity",
        "ic_counterparty",
        "ic_source",
        "account_code",
        "account_name",
        "net_amount",
        config.netsuite.fields.currency,
    ]].head(10).to_string())

    print(f"\nIC DataFrame shape: {ic_df.shape}")
    print(f"Columns: {list(ic_df.columns)}")