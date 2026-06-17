# =============================================================================
# R2R INTERCOMPANY RECONCILIATION ENGINE
# src/reporting/reporter.py
#
# What this file does:
#   Orchestrates report generation from the database.
#   Reads all data from the repository (never re-processes the CSV).
#   Calls excel_builder.py to construct the workbook.
#   Optionally uploads to Google Drive.
#   Optionally updates the Google Sheets live dashboard.
#
#   Two modes:
#     1. Post-run reporting (called by run_pipeline.py after matching)
#        Receives the MatchResult directly — no database query needed.
#        Fast — data is already in memory.
#
#     2. On-demand reporting (called by run_report.py)
#        Reads from database for any period.
#        Can regenerate the report for any past period.
#        Used by controllers who want a fresh copy mid-month.
#
# How other files use it:
#   from src.reporting.reporter import generate_report
#   output_path = generate_report(period, result, meta, run_id)
# =============================================================================

import os
from datetime import datetime
from typing import Optional

import pandas as pd

from src.config import config
from src.matching.engine import MatchResult
from src.ingestion.ingestor import IngestionMeta
from src.persistence.repository import Repository
from src.reporting.excel_builder import build_excel


# -----------------------------------------------------------------------------
# GOOGLE DRIVE UPLOADER
# Uploads the Excel file to Google Drive after generation
# Skipped if Google Drive is not configured
# -----------------------------------------------------------------------------

def _upload_to_drive(file_path: str) -> Optional[str]:
    """
    Uploads the Excel file to the configured Google Drive folder.

    Requires:
        GOOGLE_APPLICATION_CREDENTIALS or WIF token in environment
        GOOGLE_DRIVE_FOLDER_ID in environment or config

    Returns:
        Google Drive file URL if successful, None if skipped or failed.
    """
    folder_id = os.environ.get(
        "GOOGLE_DRIVE_FOLDER_ID",
        config.output.__dict__.get("google_drive_folder_id", ""),
    )

    if not folder_id:
        print("[REPORTER] Google Drive not configured. Skipping upload.")
        return None

    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from google.oauth2 import service_account
        import google.auth

        # Use application default credentials (WIF on GitHub Actions)
        credentials, project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/drive.file"]
        )

        service = build("drive", "v3", credentials=credentials)

        file_name = os.path.basename(file_path)
        file_metadata = {
            "name":    file_name,
            "parents": [folder_id],
        }
        media = MediaFileUpload(
            file_path,
            mimetype=(
                "application/vnd.openxmlformats-officedocument"
                ".spreadsheetml.sheet"
            ),
        )

        uploaded = service.files().create(
            body=          file_metadata,
            media_body=    media,
            fields=        "id, webViewLink",
        ).execute()

        url = uploaded.get("webViewLink", "")
        print(f"[REPORTER] Uploaded to Google Drive: {url}")
        return url

    except Exception as e:
        print(f"[REPORTER] Google Drive upload failed: {e}")
        print("[REPORTER] Excel file saved locally instead.")
        return None


# -----------------------------------------------------------------------------
# GOOGLE SHEETS DASHBOARD UPDATER
# Updates the live Google Sheets dashboard with summary data
# Uses Developer Metadata for drift detection (see research doc)
# -----------------------------------------------------------------------------

def _update_sheets_dashboard(
    period:       str,
    result:       MatchResult,
    repo:         Repository,
) -> bool:
    """
    Updates the live Google Sheets dashboard after a pipeline run.

    Updates two tabs:
        Dashboard:       Summary KPIs — STP rate, exception counts
        Live Exceptions: Current open exceptions table

    Uses incremental delta push via Developer Metadata to avoid
    wiping controller notes and conditional formatting.

    Returns True if successful, False if skipped or failed.
    """
    spreadsheet_id = os.environ.get(
        "GOOGLE_SHEETS_ID",
        "",
    )

    if not spreadsheet_id:
        print("[REPORTER] Google Sheets not configured. Skipping dashboard update.")
        return False

    try:
        from googleapiclient.discovery import build
        import google.auth

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        service = build("sheets", "v4", credentials=credentials)

        # --- Update Dashboard tab ---
        dashboard_values = [
            ["R2R APAC Intercompany Reconciliation — Live Dashboard"],
            [""],
            ["Period",           result.period],
            ["Last updated",     datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")],
            ["STP Rate",         f"{result.stp_rate:.1%}"],
            ["STP vs previous",  f"{result.stp_delta:+.1%}"],
            ["Matched pairs",    (result.tier1_matches + result.tier2_matches +
                                  result.tier3_matches + result.tier4_matches)],
            ["Open exceptions",  result.total_exceptions],
            ["P1 exceptions",    "See Live Exceptions tab"],
            ["LLM suggestions",  result.llm_suggestions],
            [""],
            ["Tier 1 (tranid)",  result.tier1_matches],
            ["Tier 2 (hash)",    result.tier2_matches],
            ["Tier 3 (tolerance)", result.tier3_matches],
            ["Tier 4 (subset sum)", result.tier4_matches],
            [""],
            ["Exception Types",  ""],
            ["ORPHAN",           result.orphans],
            ["TIMING_GAP",       result.timing_gaps],
            ["AMOUNT_MISMATCH",  result.amount_mismatches],
            ["ACCOUNT_MISMATCH", result.account_mismatches],
            ["CURRENCY_MISMATCH", result.currency_mismatches],
        ]

        service.spreadsheets().values().update(
            spreadsheetId=  spreadsheet_id,
            range=          "Dashboard!A1",
            valueInputOption="RAW",
            body=           {"values": dashboard_values},
        ).execute()

        # --- Update Live Exceptions tab ---
        exceptions_df = repo.get_exceptions(period)
        if len(exceptions_df) > 0:
            exc_headers = [
                "Priority", "Exception Type", "Source Entity",
                "Target CSEG", "FX Amount", "Currency",
                "Days Aged", "Status", "LLM Suggested",
            ]
            exc_values = [exc_headers]
            for _, row in exceptions_df.iterrows():
                exc_values.append([
                    str(row.get("priority", "")),
                    str(row.get("exception_type", "")),
                    str(row.get("source_entity", "")),
                    str(row.get("target_cseg", "")),
                    str(row.get("unmatched_fxamount", "")),
                    str(row.get("unmatched_currency", "")),
                    str(row.get("days_aged", "")),
                    str(row.get("status", "")),
                    str(row.get("llm_suggested_entity", "")),
                ])

            service.spreadsheets().values().update(
                spreadsheetId=  spreadsheet_id,
                range=          "Live Exceptions!A1",
                valueInputOption="RAW",
                body=           {"values": exc_values},
            ).execute()

        print(f"[REPORTER] Google Sheets dashboard updated.")
        return True

    except Exception as e:
        print(f"[REPORTER] Google Sheets update failed: {e}")
        return False


# -----------------------------------------------------------------------------
# ENTITY PAIR SUMMARY BUILDER
# Constructs the Tab 0 entity pair table from matched pairs and exceptions
# -----------------------------------------------------------------------------

def _build_entity_pair_summary(
    matched_df:    pd.DataFrame,
    exceptions_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Builds the entity pair summary table for Tab 0.

    Columns: Entity A, Entity B, Matched Pairs, Open Exceptions,
             Match Rate, Largest Exception USD

    Scalable: works for 5 to 50 entity pairs with no changes.
    """
    rows = []

    # Get all entity pairs from matches
    pair_stats = {}

    if matched_df is not None and len(matched_df) > 0:
        for _, row in matched_df.iterrows():
            key = tuple(sorted([
                str(row.get("orig_entity", "")),
                str(row.get("recv_entity", "")),
            ]))
            if key not in pair_stats:
                pair_stats[key] = {"matched": 0, "exceptions": 0, "largest": 0.0}
            pair_stats[key]["matched"] += 1

    if exceptions_df is not None and len(exceptions_df) > 0:
        for _, row in exceptions_df.iterrows():
            entity_a = str(row.get("source_entity", ""))
            entity_b = str(row.get("target_cseg", ""))
            if entity_a and entity_b:
                key = tuple(sorted([entity_a, entity_b]))
                if key not in pair_stats:
                    pair_stats[key] = {"matched": 0, "exceptions": 0, "largest": 0.0}
                pair_stats[key]["exceptions"] += 1
                amt = float(row.get("unmatched_fxamount", 0) or 0)
                pair_stats[key]["largest"] = max(pair_stats[key]["largest"], amt)

    for (entity_a, entity_b), stats in sorted(pair_stats.items()):
        total = stats["matched"] + stats["exceptions"]
        match_rate = f"{stats['matched']/total:.0%}" if total > 0 else "N/A"
        rows.append({
            "Entity A":          entity_a,
            "Entity B":          entity_b,
            "Matched Pairs":     stats["matched"],
            "Open Exceptions":   stats["exceptions"],
            "Match Rate":        match_rate,
            "Largest Exception": f"{stats['largest']:,.2f}" if stats["largest"] > 0 else "-",
        })

    return pd.DataFrame(rows)


# -----------------------------------------------------------------------------
# MAIN REPORT GENERATOR
# Single entry point — called by run_pipeline.py and run_report.py
# -----------------------------------------------------------------------------

def generate_report(
    period:      str,
    result:      MatchResult,
    meta:        IngestionMeta,
    run_id:      str,
    output_dir:  str = "output",
    upload:      bool = True,
) -> str:
    """
    Generates the complete period reconciliation report.

    Args:
        period:     Period string e.g. "Jun 2026"
        result:     MatchResult from engine.py (contains matched + exceptions)
        meta:       IngestionMeta from ingestor.py
        run_id:     Unique run identifier
        output_dir: Local directory for Excel output
        upload:     Whether to upload to Google Drive

    Returns:
        Local path to the generated Excel file.
    """
    print(f"\n[REPORTER] Generating report for {period}...")

    repo = Repository()

    # Read data from database
    # (already saved by repository.py during the pipeline run)
    matched_df    = repo.get_matched_pairs(period)
    exceptions_df = repo.get_exceptions(period)
    je_drafts_df  = repo.get_je_drafts(period)

    # If DataFrames are empty, use result DataFrames directly
    # (handles the case where database write hasn't completed yet)
    if (matched_df is None or len(matched_df) == 0) and \
       result.matched_pairs is not None and len(result.matched_pairs) > 0:
        matched_df = result.matched_pairs
        print("[REPORTER] Using in-memory matched pairs (database not yet written)")

    if (exceptions_df is None or len(exceptions_df) == 0) and \
       result.exceptions is not None and len(result.exceptions) > 0:
        exceptions_df = result.exceptions
        print("[REPORTER] Using in-memory exceptions (database not yet written)")

    if (je_drafts_df is None or len(je_drafts_df) == 0) and \
       result.je_drafts is not None and len(result.je_drafts) > 0:
        je_drafts_df = result.je_drafts
        print("[REPORTER] Using in-memory JE drafts (database not yet written)")
           
    # Build entity pair summary for Tab 0
    entity_pairs = _build_entity_pair_summary(matched_df, exceptions_df)

    # Build Excel workbook
    output_path = build_excel(
        matched_df=    matched_df,
        exceptions_df= exceptions_df,
        je_drafts_df=  je_drafts_df,
        entity_pairs=  entity_pairs,
        result=        result,
        meta=          meta,
        output_dir=    output_dir,
    )

    # Upload to Google Drive
    drive_url = None
    if upload:
        drive_url = _upload_to_drive(output_path)

    # Update Google Sheets dashboard
    if upload:
        _update_sheets_dashboard(period, result, repo)

    print(
        f"\n[REPORTER] Report complete."
        f"\n  Local:  {output_path}"
        f"\n  Drive:  {drive_url or 'Not uploaded'}"
    )
          
    return output_path


# -----------------------------------------------------------------------------
# ON-DEMAND REPORT (re-generates from database for any past period)
# Called by run_report.py
# -----------------------------------------------------------------------------

def generate_report_from_db(
    period:     str,
    output_dir: str = "output",
    upload:     bool = True,
) -> str:
    """
    Regenerates a report for any past period from the database.
    Does not require re-running the matching engine.

    Used when:
    - Controller wants a fresh copy mid-month
    - Prior period report needs regenerating
    - Demo with historical data

    Args:
        period:     Period string e.g. "Jun 2026"
        output_dir: Local directory for Excel output
        upload:     Whether to upload to Google Drive
    """
    print(f"\n[REPORTER] Regenerating report from database for {period}...")

    repo = Repository()

    matched_df    = repo.get_matched_pairs(period)
    exceptions_df = repo.get_exceptions(period)
    je_drafts_df  = repo.get_je_drafts(period)
    run_log_df    = repo.get_run_log(period)

    if run_log_df is None or len(run_log_df) == 0:
        raise ValueError(
            f"No run log found for period '{period}'. "
            f"Has the pipeline been run for this period?"
        )

    # Reconstruct MatchResult from database
    latest_run = run_log_df.iloc[0]
    result = MatchResult(
        run_id=           str(latest_run.get("run_id", "")),
        period=           period,
        started_at=       latest_run.get("started_at", datetime.utcnow()),
        completed_at=     latest_run.get("completed_at"),
        total_ic_rows=    int(latest_run.get("ic_identified", 0) or 0),
        tier1_matches=    int(latest_run.get("tier1_matches", 0) or 0),
        tier2_matches=    int(latest_run.get("tier2_matches", 0) or 0),
        tier3_matches=    int(latest_run.get("tier3_matches", 0) or 0),
        tier4_matches=    int(latest_run.get("tier4_matches", 0) or 0),
        llm_suggestions=  int(latest_run.get("llm_suggestions", 0) or 0),
        total_exceptions= int(latest_run.get("exceptions_open", 0) or 0),
        orphans=          0,
        timing_gaps=      0,
        amount_mismatches=0,
        account_mismatches=0,
        currency_mismatches=0,
        other_exceptions= 0,
        stp_rate=         float(latest_run.get("stp_rate", 0) or 0),
        stp_previous_run= float(latest_run.get("stp_previous_run", 0) or 0),
        stp_delta=        float(latest_run.get("stp_delta", 0) or 0),
        stp_alert_fired=  bool(latest_run.get("stp_alert_fired", False)),
        input_file_md5=   str(latest_run.get("input_md5", "")),
    )

    # Reconstruct exception type counts from exceptions_df
    if exceptions_df is not None and len(exceptions_df) > 0:
        exc_counts = exceptions_df["exception_type"].value_counts().to_dict()
        result.orphans           = exc_counts.get("ORPHAN", 0)
        result.timing_gaps       = exc_counts.get("TIMING_GAP", 0)
        result.amount_mismatches = exc_counts.get("AMOUNT_MISMATCH", 0)
        result.account_mismatches= exc_counts.get("ACCOUNT_MISMATCH", 0)
        result.currency_mismatches=exc_counts.get("CURRENCY_MISMATCH", 0)

    # Reconstruct IngestionMeta (minimal — for report header only)
    meta = IngestionMeta(
        file_path=          str(latest_run.get("input_file", "")),
        file_md5=           str(latest_run.get("input_md5", "")),
        ingested_at=        latest_run.get("started_at", datetime.utcnow()),
        total_rows_raw=     int(latest_run.get("records_in", 0) or 0),
        total_rows_ic=      int(latest_run.get("ic_identified", 0) or 0),
        rows_non_ic=        0,
        rows_out_of_scope=  int(latest_run.get("out_of_scope", 0) or 0),
        rows_reversed=      int(latest_run.get("reversals_dropped", 0) or 0),
        rows_fx_coalesced=  0,
        rows_cseg_corrected=0,
        rows_memo_identified=0,
        period=             period,
        encoding_used=      "utf-8",
    )

    entity_pairs = _build_entity_pair_summary(matched_df, exceptions_df)

    output_path = build_excel(
        matched_df=    matched_df,
        exceptions_df= exceptions_df,
        je_drafts_df=  je_drafts_df,
        entity_pairs=  entity_pairs,
        result=        result,
        meta=          meta,
        output_dir=    output_dir,
    )

    if upload:
        _upload_to_drive(output_path)
        _update_sheets_dashboard(period, result, repo)

    print(f"[REPORTER] On-demand report complete: {output_path}")
    return output_path
