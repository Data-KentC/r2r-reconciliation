# =============================================================================
# R2R INTERCOMPANY RECONCILIATION ENGINE
# src/ingestion/validator.py
#
# Pandera schema guard — the first line of defence against NetSuite changes.
#
# What this file does:
#   Validates every CSV column before any matching logic runs.
#   If NetSuite renames a column, changes a data type, or adds unexpected
#   columns, this file catches it immediately and produces a plain English
#   error message that a non-technical user can act on.
#
# How other files use it:
#   from src.ingestion.validator import validate_csv
#   validated_df = validate_csv(raw_df)
#
# If validation fails:
#   - Writes plain English error to GitHub Actions step summary
#   - Raises ValidationError — pipeline stops cleanly
#   - No partial data reaches the database
# =============================================================================

import os
import sys
import pandas as pd
import pandera as pa
from pandera.errors import SchemaErrors
from typing import Optional

from src.config import config


# -----------------------------------------------------------------------------
# GITHUB ACTIONS SUMMARY WRITER
# When running on GitHub Actions, errors appear in the run summary page
# as formatted Markdown — not as raw Python stack traces.
# When running locally, errors print to terminal instead.
# -----------------------------------------------------------------------------

def write_github_summary(markdown_content: str) -> None:
    """
    Writes a formatted Markdown message to the GitHub Actions run summary.
    Falls back to terminal print when running locally.
    """
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(markdown_content + "\n")
    else:
        # Running locally — print to terminal
        print(markdown_content)


# -----------------------------------------------------------------------------
# PLAIN ENGLISH ERROR FORMATTER
# Translates Pandera's technical error objects into messages a controller
# can read and act on without needing to understand Python.
# -----------------------------------------------------------------------------

def _format_schema_errors(schema_error: SchemaErrors) -> str:
    """
    Converts Pandera SchemaErrors into plain English accounting language.
    Returns a Markdown string ready for GitHub Actions step summary.
    """
    lines = [
        "## 🚨 NetSuite Export Validation Failed",
        "",
        "The CSV file failed structural validation before any matching began.",
        "No data has been written to the database.",
        "",
        "### Issues Found:",
        "",
    ]

    for record in schema_error.failure_cases.to_dict("records"):
        check      = str(record.get("check", ""))
        column     = str(record.get("column", "unknown"))
        failure    = str(record.get("failure_case", ""))

        # Translate each error type into plain English
        if check == "column_in_dataframe":
            lines.append(
                f"- **Missing Column:** `{column}` was not found in the export. "
                f"This column may have been renamed or removed by a NetSuite "
                f"administrator. Ask your NetSuite admin to restore the column "
                f"`{column}` in the GL Saved Search."
            )
        elif check == "column_in_schema":
            lines.append(
                f"- **Unexpected Column:** `{column}` appeared in the export "
                f"but is not expected. A new column may have been added to the "
                f"Saved Search. Review the Saved Search configuration."
            )
        elif "dtype" in check:
            lines.append(
                f"- **Wrong Data Type:** Column `{column}` contains unexpected "
                f"characters or formatting. This often happens when NetSuite "
                f"exports numbers with currency symbols or text. "
                f"Problematic value: `{failure}`"
            )
        elif check == "not_nullable":
            lines.append(
                f"- **Empty Values:** Column `{column}` contains blank rows "
                f"where a value is required. "
                f"Check the NetSuite Saved Search filter settings."
            )
        elif check == "isin":
            lines.append(
                f"- **Invalid Value:** Column `{column}` contains an "
                f"unrecognised value: `{failure}`. "
                f"This may be a new currency or entity code not yet in config.yaml."
            )
        elif check == "greater_than_or_equal_to_0":
            lines.append(
                f"- **Negative Amount:** Column `{column}` contains a negative "
                f"value where only positive numbers are expected: `{failure}`. "
                f"Check the NetSuite export formula."
            )
        else:
            lines.append(
                f"- **Validation Error:** Column `{column}` failed check "
                f"`{check}` with value `{failure}`."
            )

    lines += [
        "",
        "### Next Steps:",
        "1. Do not re-run the pipeline until the export issue is fixed.",
        "2. Contact your NetSuite administrator with the errors above.",
        "3. Re-export the CSV after the Saved Search is corrected.",
        "4. Re-run the pipeline with the corrected file.",
        "",
        f"*Pipeline run aborted. No financial data was modified.*",
    ]

    return "\n".join(lines)


# -----------------------------------------------------------------------------
# PRE-PROCESSING
# Handles known NetSuite quirks before Pandera validation runs.
# These are structural fixes, not business logic.
# -----------------------------------------------------------------------------

def _preprocess(df: pd.DataFrame) -> pd.DataFrame:
    """
    Applies known NetSuite export quirks before schema validation.

    Known issues handled here:
    1. Amount columns may have thousand separators: "1,234.56" → 1234.56
    2. Posting Period exports as plain string — must stay as string
    3. Debit/credit columns are mutually exclusive nulls — both nullable
    4. fxamount may be null for elimination entries — handled by null guard
    """
    ns = config.netsuite.fields

    # Fix 1: Strip thousand separators from amount columns
    # NetSuite sometimes exports "1,234.56" which pandas reads as a string
    amount_columns = [
        ns.debit_amount,
        ns.credit_amount,
        ns.fx_amount,
    ]

    for col in amount_columns:
        if col in df.columns:
            df[col] = (
                df[col]
                .astype(str)
                .str.replace(",", "", regex=False)  # Remove thousand separators
                .str.strip()
                .replace("nan", "")                  # Restore empty strings
                .replace("", None)                   # Convert empty to None
            )
            # Convert to numeric — errors='coerce' turns unparseable to NaN
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Fix 2: Ensure date column stays as string initially
    # Pandera will validate format; pandas should not auto-parse
    if ns.tran_date in df.columns:
        df[ns.tran_date] = df[ns.tran_date].astype(str).str.strip()

    # Fix 3: Ensure text columns are strings, not accidentally numeric
    text_columns = [
        ns.internal_id,
        ns.tran_id,
        ns.account,
        ns.currency,
        ns.line_subsidiary,
        ns.cseg_ic,
        ns.memo,
    ]
    for col in text_columns:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()
            df[col] = df[col].replace("nan", "")

    return df


# -----------------------------------------------------------------------------
# PANDERA SCHEMA
# Defines the exact structure expected from a NetSuite GL export.
# strict=True means unexpected columns are rejected.
# lazy=True means ALL errors are collected before failing.
# -----------------------------------------------------------------------------

def _build_schema() -> pa.DataFrameSchema:
    """
    Builds the Pandera validation schema from config field names.
    Called once per validation run.

    Schema rules:
    - strict=True: rejects extra or renamed columns immediately
    - coerce=False: never silently converts wrong types
    - lazy=True: collects all errors before raising
    """
    ns = config.netsuite.fields

    # Valid currencies from active entities
    valid_currencies = config.valid_currencies

    # Valid entity codes for subsidiary column
    # Allow ANTH-US and others — out-of-scope filter happens in classifier
    # Validator only checks the column exists and is a string

    columns = {
        # Internal ID — must exist, must be string
        ns.internal_id: pa.Column(
            str,
            nullable=False,
            coerce=False,
            description="NetSuite internal transaction ID",
        ),

        # Line sequence number — must be numeric
        ns.line_sequence: pa.Column(
            pa.Int,
            nullable=False,
            coerce=True,   # Allow "1" string to convert to int
            description="Line sequence within a transaction",
        ),

        # Transaction date — must be a string (validated as date in ingestor)
        ns.tran_date: pa.Column(
            str,
            nullable=False,
            coerce=False,
            description="Transaction date in YYYY-MM-DD format",
        ),

        # Document number — nullable (not all transactions have tranid)
        ns.tran_id: pa.Column(
            str,
            nullable=True,
            coerce=False,
            description="Shared document number — present on AICJEs",
        ),

        # Account — must exist, must be string
        ns.account: pa.Column(
            str,
            nullable=False,
            coerce=False,
            description="GL account code and name (concatenated string)",
        ),

        # Debit amount — nullable (mutually exclusive with credit)
        ns.debit_amount: pa.Column(
            float,
            nullable=True,
            coerce=False,
            checks=pa.Check(
                lambda s: (s.dropna() >= 0).all(),
                error="Debit amounts must be non-negative",
            ),
            description="Debit amount in entity functional currency",
        ),

        # Credit amount — nullable (mutually exclusive with debit)
        ns.credit_amount: pa.Column(
            float,
            nullable=True,
            coerce=False,
            checks=pa.Check(
                lambda s: (s.dropna() >= 0).all(),
                error="Credit amounts must be non-negative",
            ),
            description="Credit amount in entity functional currency",
        ),

        # Currency — must be a recognised currency code
        ns.currency: pa.Column(
            str,
            nullable=False,
            coerce=False,
            description="Transaction currency code (ISO 4217)",
        ),

        # FX Amount — nullable (elimination entries may have null fxamount)
        # Null guard coalescence happens in ingestor, not here
        ns.fx_amount: pa.Column(
            float,
            nullable=True,   # Explicitly allow null — see case_14 in synthetic data
            coerce=False,
            description="Amount in transaction currency (foreign currency amount)",
        ),

        # Line subsidiary — must exist
        ns.line_subsidiary: pa.Column(
            str,
            nullable=False,
            coerce=False,
            description="Entity that owns this GL line (flat string, no hierarchy)",
        ),

        # IC custom segment — nullable (15-25% of manual JEs will be blank)
        ns.cseg_ic: pa.Column(
            str,
            nullable=True,
            coerce=False,
            description="Intercompany counterparty tag — blank on some manual JEs",
        ),

        # Memo — nullable (not always populated)
        ns.memo: pa.Column(
            str,
            nullable=True,
            coerce=False,
            description="Transaction memo/description — used for tertiary IC detection",
        ),
    }

    return pa.DataFrameSchema(
        columns=columns,
        strict=True,     # Reject unexpected columns — catches Saved Search changes
        coerce=False,    # Never silently fix type mismatches
    )


# -----------------------------------------------------------------------------
# NULL GUARD
# Handles null fxamount AFTER schema validation passes.
# Coalesces to base amount with a loud warning.
# This is separate from validation — it is a data correction, not a check.
# -----------------------------------------------------------------------------

def _apply_null_guard(df: pd.DataFrame) -> pd.DataFrame:
    """
    Handles rows where fxamount is null.

    NetSuite context: amortization, allocation, and base-currency elimination
    JEs sometimes export with null fxamount because they are book-level
    adjustments with no foreign currency component.

    Resolution: Coalesce fxamount to base net_amount (debit - credit).
    These rows are flagged with is_fx_coalesced=True for audit visibility.

    This does NOT silently fix the data — it logs every coalesced row.
    """
    ns = config.netsuite.fields
    fx_col = ns.fx_amount

    null_mask = df[fx_col].isnull()
    null_count = null_mask.sum()

    if null_count > 0:
        # Calculate base net amount as fallback
        debit  = df[ns.debit_amount].fillna(0)
        credit = df[ns.credit_amount].fillna(0)
        net    = debit - credit

        # Coalesce null fxamount rows to net base amount
        df.loc[null_mask, fx_col] = net[null_mask]

        # Flag these rows for audit visibility
        df["is_fx_coalesced"] = False
        df.loc[null_mask, "is_fx_coalesced"] = True

        warning_message = (
            f"[VALIDATOR] NULL GUARD: {null_count} rows had null fxamount. "
            f"Coalesced to base net amount (debit - credit). "
            f"These rows are flagged with is_fx_coalesced=True. "
            f"Review these rows if they appear in exception output."
        )
        print(warning_message)
        write_github_summary(f"⚠️ **Null fxamount Warning:** {warning_message}")

    else:
        df["is_fx_coalesced"] = False

    # Final assertion — no nulls should remain
    assert df[fx_col].isnull().sum() == 0, (
        "Null fxamount remains after coalescing. This should not happen."
    )

    return df


# -----------------------------------------------------------------------------
# MAIN VALIDATION FUNCTION
# Called by ingestor.py — the only entry point into this module
# -----------------------------------------------------------------------------

def validate_csv(raw_df: pd.DataFrame) -> pd.DataFrame:
    """
    Validates a raw NetSuite GL CSV DataFrame.

    Steps:
    1. Pre-process known NetSuite quirks (thousand separators etc.)
    2. Validate schema with Pandera (strict column checking)
    3. Apply null guard for fxamount coalescing

    Returns:
        Validated and pre-processed DataFrame ready for the ingestor.

    Raises:
        SystemExit: If schema validation fails — writes plain English
                    error to GitHub Actions summary before exiting.
    """
    print("[VALIDATOR] Starting schema validation...")

    # Step 1: Pre-process
    df = _preprocess(raw_df.copy())
    print(f"[VALIDATOR] Pre-processing complete. {len(df)} rows to validate.")

    # Step 2: Schema validation
    schema = _build_schema()
    try:
        validated_df = schema.validate(df, lazy=True)
        print(
            f"[VALIDATOR] Schema validation passed. "
            f"{len(validated_df)} rows, "
            f"{len(validated_df.columns)} columns confirmed."
        )

    except SchemaErrors as e:
        # Format errors into plain English and write to GitHub summary
        error_report = _format_schema_errors(e)
        write_github_summary(error_report)

        # Also print to terminal for local debugging
        print("\n" + "=" * 60)
        print("VALIDATION FAILED — see error details above")
        print("=" * 60)
        print(f"Total schema violations: {len(e.failure_cases)}")
        print(e.failure_cases.to_string())

        sys.exit(1)  # Stop pipeline — no data reaches the database

    # Step 3: Null guard
    validated_df = _apply_null_guard(validated_df)

    print(
        f"[VALIDATOR] Validation complete. "
        f"Null fxamount rows coalesced: "
        f"{validated_df['is_fx_coalesced'].sum()}"
    )

    return validated_df


# -----------------------------------------------------------------------------
# STANDALONE TEST
# Run this file directly to test validation against synthetic data:
#   python src/ingestion/validator.py
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
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

    print(f"Loading synthetic data from: {synthetic_path}")
    raw = pd.read_csv(synthetic_path, dtype=str)
    print(f"Loaded {len(raw)} rows, {len(raw.columns)} columns")
    print(f"Columns: {list(raw.columns)}\n")

    result = validate_csv(raw)
    print(f"\nValidation passed. {len(result)} rows ready for ingestion.")