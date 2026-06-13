# =============================================================================
# R2R INTERCOMPANY RECONCILIATION ENGINE
# jobs/run_report.py
#
# What this file does:
#   On-demand report generator.
#   Reads from the database and regenerates the Excel report
#   for any period without re-running the matching engine.
#
#   When to use this:
#     - Controller wants a fresh report mid-month
#     - Prior period report needs regenerating
#     - Demo with historical data from a previous run
#     - CFO asks for the latest numbers outside of scheduled runs
#
#   How it works:
#     Reads matched pairs, exceptions, and JE drafts from the database.
#     Reconstructs the MatchResult from the run_log table.
#     Calls reporter.py to build the Excel file.
#     Optionally uploads to Google Drive.
#
#   This file is also triggered by a GitHub Actions workflow
#   (.github/workflows/generate_report.yml) so any authorised
#   collaborator can generate a report from the GitHub UI
#   without opening a terminal.
#
# How to run locally:
#   python jobs/run_report.py --period "Jun 2026"
#   python jobs/run_report.py --period "Jun 2026" --no-upload
#   python jobs/run_report.py --list-periods
# =============================================================================

import argparse
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.config import config
from src.persistence.database import init_db, test_connection
from src.persistence.repository import Repository
from src.reporting.reporter import generate_report_from_db


# -----------------------------------------------------------------------------
# LIST AVAILABLE PERIODS
# Shows all periods that have been processed and are in the database
# -----------------------------------------------------------------------------

def list_available_periods() -> None:
    """
    Lists all periods available in the database for report generation.
    """
    try:
        from src.persistence.database import get_engine
        from sqlalchemy import text

        engine = get_engine()
        with engine.connect() as conn:
            result = conn.execute(text(
                "SELECT DISTINCT period, COUNT(*) as runs, "
                "MAX(started_at) as last_run "
                "FROM run_log "
                "WHERE status = 'COMPLETE' "
                "GROUP BY period "
                "ORDER BY last_run DESC"
            ))
            rows = result.fetchall()

        if not rows:
            print("\nNo completed pipeline runs found in database.")
            print("Run the pipeline first: python jobs/run_pipeline.py --file <csv>")
            return

        print("\nAvailable periods in database:")
        print(f"{'Period':<15} {'Runs':<8} {'Last Run'}")
        print("-" * 45)
        for row in rows:
            print(
                f"{row[0]:<15} "
                f"{row[1]:<8} "
                f"{str(row[2])[:19]}"
            )

    except Exception as e:
        print(f"Could not query database: {e}")
        print("Ensure database is configured and pipeline has been run.")


# -----------------------------------------------------------------------------
# MAIN REPORT GENERATOR
# -----------------------------------------------------------------------------

def run_report(
    period:    str,
    upload:    bool = True,
    output_dir: str = "output",
) -> int:
    """
    Generates an on-demand Excel report for a given period.

    Args:
        period:     Period string e.g. "Jun 2026"
        upload:     Whether to upload to Google Drive
        output_dir: Local output directory

    Returns:
        Exit code: 0 = success, 1 = failure
    """
    start_time = time.time()

    print("\n" + "=" * 60)
    print("R2R REPORT GENERATOR (ON-DEMAND)")
    print("=" * 60)
    print(f"Period:     {period}")
    print(f"Upload:     {'Yes' if upload else 'No'}")
    print(f"Started:    {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60 + "\n")

    try:
        # Check database connection
        if not test_connection():
            print("FATAL: Database connection failed.")
            print(
                "Check SUPABASE_POOLER_URL or ensure SQLite "
                "fallback is enabled in config.yaml."
            )
            return 1

        init_db()

        # Generate report from database
        output_path = generate_report_from_db(
            period=     period,
            output_dir= output_dir,
            upload=     upload,
        )

        runtime = time.time() - start_time
        print("\n" + "=" * 60)
        print("REPORT COMPLETE")
        print("=" * 60)
        print(f"  Period:   {period}")
        print(f"  Output:   {output_path}")
        print(f"  Runtime:  {runtime:.1f}s")
        print("=" * 60 + "\n")

        return 0

    except ValueError as e:
        # Period not found in database
        print(f"\nERROR: {e}")
        print("\nAvailable periods:")
        list_available_periods()
        return 1

    except Exception as e:
        print(f"\nFATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1


# -----------------------------------------------------------------------------
# ENTRY POINT
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="R2R APAC Intercompany Reconciliation — On-Demand Report Generator"
    )

    parser.add_argument(
        "--period", "-p",
        type=str,
        default=None,
        help=(
            "Period to generate report for. "
            "Format: 'Jun 2026' or '2026-06'. "
            "Required unless --list-periods is used."
        ),
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        default=False,
        help="Skip Google Drive upload. Report saved locally only.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="output",
        help="Local directory for Excel output. Default: output/",
    )
    parser.add_argument(
        "--list-periods",
        action="store_true",
        default=False,
        help="List all available periods in the database and exit.",
    )

    args = parser.parse_args()

    # Handle --list-periods
    if args.list_periods:
        try:
            from src.persistence.database import init_db, test_connection
            if test_connection():
                init_db()
                list_available_periods()
            else:
                print("Database connection failed.")
        except Exception as e:
            print(f"Error: {e}")
        sys.exit(0)

    # Require --period if not listing
    if not args.period:
        print("ERROR: --period is required.")
        print("Usage: python jobs/run_report.py --period 'Jun 2026'")
        print("       python jobs/run_report.py --list-periods")
        sys.exit(1)

    # Normalise period format
    period = args.period.strip()

    # Convert "2026-06" to "Jun 2026" if needed
    if len(period) == 7 and period[4] == "-":
        try:
            dt = datetime.strptime(period, "%Y-%m")
            period = dt.strftime("%b %Y")
        except ValueError:
            pass  # Keep as-is if not parseable

    exit_code = run_report(
        period=     period,
        upload=     not args.no_upload,
        output_dir= args.output_dir,
    )

    sys.exit(exit_code)