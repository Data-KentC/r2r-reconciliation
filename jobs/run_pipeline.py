# =============================================================================
# R2R INTERCOMPANY RECONCILIATION ENGINE
# jobs/run_pipeline.py
#
# What this file does:
#   Main entry point for the full reconciliation pipeline.
#   Called by GitHub Actions on schedule or manual trigger.
#   Orchestrates the complete sequence:
#     1. Initialise database
#     2. Check for new CSV in Gmail inbox
#     3. Ingest and validate the CSV
#     4. Save GL lines to database
#     5. Run matching engine (Tiers 1-5)
#     6. Save matched pairs and exceptions to database
#     7. Save JE drafts to database
#     8. Generate Excel report
#     9. Upload to Google Drive
#    10. Update Google Sheets dashboard
#    11. Send exception alerts to entity accountants
#    12. Complete run log
#
#   Run modes:
#     Scheduled: GitHub Actions cron (Mon/Thu 08:00 SGT)
#     Manual:    GitHub Actions workflow_dispatch (any authorised user)
#     Local:     python jobs/run_pipeline.py --file path/to/file.csv
#
#   Environment variables required:
#     SUPABASE_POOLER_URL   Database connection (or SQLite fallback)
#     GEMINI_API_KEY        LLM API key
#     GROQ_API_KEY          LLM API key (primary)
#     GMAIL_REFRESH_TOKEN   Gmail OAuth token (for inbox polling)
#     GMAIL_CLIENT_ID       Gmail OAuth client ID
#     GMAIL_CLIENT_SECRET   Gmail OAuth client secret
#
#   Optional environment variables:
#     GOOGLE_DRIVE_FOLDER_ID   Google Drive upload folder
#     GOOGLE_SHEETS_ID         Google Sheets dashboard
#     TELEGRAM_BOT_TOKEN       Telegram fallback notifications
#     RECON_TARGET             Entity filter (ALL_ENTITIES_APAC or subset)
#     MANUAL_TRIGGER_REASON    SOX log: reason for manual run
# =============================================================================

import argparse
import os
import sys
import time
import uuid
from datetime import datetime

# Add project root to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.config import config
from src.persistence.database import init_db, test_connection
from src.persistence.repository import Repository
from src.ingestion.ingestor import ingest
from src.matching.engine import run_matching_engine
from src.reporting.reporter import generate_report


# -----------------------------------------------------------------------------
# GMAIL CSV FETCHER
# Polls the shared Gmail inbox for a new NetSuite CSV attachment
# Falls back gracefully if Gmail API is not configured
# -----------------------------------------------------------------------------

def _fetch_csv_from_gmail() -> str | None:
    """
    Polls the configured Gmail inbox for a new NetSuite GL CSV attachment.

    Searches for emails from the NetSuite sender address in config.yaml.
    Downloads the CSV attachment to data/incoming/.
    Marks the email as processed (moves to archive label).

    Returns:
        Local file path if CSV found and downloaded.
        None if no new CSV available or Gmail not configured.
    """
    gmail_token   = os.environ.get("GMAIL_REFRESH_TOKEN", "")
    client_id     = os.environ.get("GMAIL_CLIENT_ID", "")
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET", "")

    if not all([gmail_token, client_id, client_secret]):
        print(
            "[PIPELINE] Gmail credentials not configured. "
            "To use Gmail inbox polling, set GMAIL_REFRESH_TOKEN, "
            "GMAIL_CLIENT_ID, and GMAIL_CLIENT_SECRET. "
            "For now, use --file argument to specify CSV manually."
        )
        return None

    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        import base64

        creds = Credentials(
            token=         None,
            refresh_token= gmail_token,
            client_id=     client_id,
            client_secret= client_secret,
            token_uri=     "https://oauth2.googleapis.com/token",
            scopes=        ["https://www.googleapis.com/auth/gmail.modify"],
        )

        service = build("gmail", "v1", credentials=creds)

        # Search for unread emails from NetSuite sender
        netsuite_sender = config.notifications.gmail.__dict__.get(
            "netsuite_sender", ""
        )

        query = f"from:{netsuite_sender} has:attachment is:unread"
        results = service.users().messages().list(
            userId="me", q=query
        ).execute()

        messages = results.get("messages", [])

        if not messages:
            print("[PIPELINE] No new NetSuite emails found in Gmail inbox.")
            return None

        # Process the most recent email
        msg_id  = messages[0]["id"]
        message = service.users().messages().get(
            userId="me", id=msg_id
        ).execute()

        # Find CSV attachment
        parts = message.get("payload", {}).get("parts", [])
        for part in parts:
            filename = part.get("filename", "")
            if not filename.endswith(".csv"):
                continue

            # Download attachment
            attachment_id = part["body"].get("attachmentId")
            if not attachment_id:
                continue

            attachment = service.users().messages().attachments().get(
                userId="me", messageId=msg_id, id=attachment_id
            ).execute()

            data = base64.urlsafe_b64decode(attachment["data"])

            # Save to incoming folder
            incoming_dir = "data/incoming"
            os.makedirs(incoming_dir, exist_ok=True)

            timestamp   = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            local_path  = os.path.join(incoming_dir, f"{timestamp}_{filename}")

            with open(local_path, "wb") as f:
                f.write(data)

            print(f"[PIPELINE] CSV downloaded from Gmail: {local_path}")

            # Mark email as read and archive
            service.users().messages().modify(
                userId="me",
                id=msg_id,
                body={
                    "removeLabelIds": ["UNREAD", "INBOX"],
                    "addLabelIds":    [],
                },
            ).execute()

            return local_path

        print("[PIPELINE] Email found but no CSV attachment detected.")
        return None

    except Exception as e:
        print(f"[PIPELINE] Gmail fetch failed: {e}")
        return None


# -----------------------------------------------------------------------------
# MAIN PIPELINE ORCHESTRATOR
# -----------------------------------------------------------------------------

def run_pipeline(
    csv_file_path:     str | None = None,
    manual_reason:     str | None = None,
    entity_filter:     str = "ALL_ENTITIES_APAC",
) -> int:
    """
    Runs the complete reconciliation pipeline.

    Args:
        csv_file_path:  Path to CSV file. If None, polls Gmail inbox.
        manual_reason:  SOX log entry for manual triggers.
        entity_filter:  Entity scope filter from workflow_dispatch dropdown.

    Returns:
        Exit code: 0 = success, 1 = failure
    """
    pipeline_start = time.time()
    run_id         = str(uuid.uuid4())

    print("\n" + "=" * 60)
    print("R2R APAC INTERCOMPANY RECONCILIATION ENGINE")
    print("=" * 60)
    print(f"Run ID:         {run_id}")
    print(f"Started:        {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Entity filter:  {entity_filter}")
    print(f"Environment:    {config.environment}")
    if manual_reason:
        print(f"Manual reason:  {manual_reason}")
    print("=" * 60 + "\n")

    repo = Repository()

    try:
        # ------------------------------------------------------------------
        # Step 1: Database initialisation
        # ------------------------------------------------------------------
        print("[PIPELINE] Step 1: Database initialisation")
        if not test_connection():
            print("[PIPELINE] FATAL: Database connection failed.")
            return 1
        init_db()

        # ------------------------------------------------------------------
        # Step 2: Get CSV file
        # ------------------------------------------------------------------
        print("\n[PIPELINE] Step 2: Locating NetSuite CSV export")

        if csv_file_path:
            print(f"[PIPELINE] Using provided file: {csv_file_path}")
        else:
            print("[PIPELINE] Polling Gmail inbox for new CSV...")
            csv_file_path = _fetch_csv_from_gmail()

        if not csv_file_path:
            print(
                "[PIPELINE] No CSV file available. "
                "Nothing to process this run."
            )
            return 0  # Not an error — may genuinely be no new data

        if not os.path.exists(csv_file_path):
            print(f"[PIPELINE] FATAL: CSV file not found: {csv_file_path}")
            return 1

        # ------------------------------------------------------------------
        # Step 3: Ingest and validate CSV
        # ------------------------------------------------------------------
        print(f"\n[PIPELINE] Step 3: Ingesting {csv_file_path}")

        ic_df, meta = ingest(csv_file_path)

        if len(ic_df) == 0:
            print(
                "[PIPELINE] No IC transactions found after ingestion. "
                "Check that the Saved Search includes IC accounts."
            )
            return 0

        # ------------------------------------------------------------------
        # Step 4: Start run log
        # ------------------------------------------------------------------
        repo.save_run_log_start(
            run_id=     run_id,
            period=     meta.period,
            file_path=  csv_file_path,
            file_md5=   meta.file_md5,
            records_in= meta.total_rows_raw,
        )

        # ------------------------------------------------------------------
        # Step 5: Save GL lines to database
        # ------------------------------------------------------------------
        print(f"\n[PIPELINE] Step 5: Saving GL lines to database")
        repo.save_gl_lines(ic_df, run_id, meta.file_md5)

        # ------------------------------------------------------------------
        # Step 6: Run matching engine
        # ------------------------------------------------------------------
        print(f"\n[PIPELINE] Step 6: Running matching engine")

        previous_stp = repo.get_previous_stp(meta.period)
        result = run_matching_engine(
            ic_df=        ic_df,
            meta=         meta,
            run_id=       run_id,
            previous_stp= previous_stp,
        )

        # ------------------------------------------------------------------
        # Step 7: Save results to database
        # ------------------------------------------------------------------
        print(f"\n[PIPELINE] Step 7: Saving results to database")

        repo.save_matched_pairs(result.matched_pairs)
        new_exc, recurring_exc, resolved_exc = repo.save_exceptions(
            result.exceptions, run_id
        )

        print(
            f"[PIPELINE] Exceptions: "
            f"{new_exc} new, {recurring_exc} recurring, "
            f"{resolved_exc} resolved"
        )

        if result.je_drafts is not None and len(result.je_drafts) > 0:
            repo.save_je_drafts(result.je_drafts)
            print(f"[PIPELINE] JE drafts saved: {len(result.je_drafts)}")
        else:
            print("[PIPELINE] No JE drafts generated this run.")

        # ------------------------------------------------------------------
        # Step 8: Generate Excel report + upload
        # ------------------------------------------------------------------
        print(f"\n[PIPELINE] Step 8: Generating report")

        output_path = generate_report(
            period=     meta.period,
            result=     result,
            meta=       meta,
            run_id=     run_id,
            output_dir= "output",
            upload=     True,
        )

        # ------------------------------------------------------------------
        # Step 9: Send exception alerts
        # ------------------------------------------------------------------
        print(f"\n[PIPELINE] Step 9: Sending exception alerts")
        _send_exception_alerts(result, run_id, repo)

        # ------------------------------------------------------------------
        # Step 10: Complete run log
        # ------------------------------------------------------------------
        runtime = time.time() - pipeline_start
        repo.complete_run_log(
            run_id=  run_id,
            result=  result,
            runtime= runtime,
            status=  "COMPLETE",
        )

        # ------------------------------------------------------------------
        # Final summary
        # ------------------------------------------------------------------
        print("\n" + "=" * 60)
        print("PIPELINE COMPLETE")
        print("=" * 60)
        print(f"  Period:        {meta.period}")
        print(f"  STP rate:      {result.stp_rate:.1%}")
        print(f"  Matched pairs: {len(result.matched_pairs)}")
        print(f"  Exceptions:    {result.total_exceptions}")
        print(f"  JE drafts:     {len(result.je_drafts)}")
        print(f"  Report:        {output_path}")
        print(f"  Runtime:       {runtime:.1f}s")
        print("=" * 60 + "\n")

        return 0

    except Exception as e:
        runtime = time.time() - pipeline_start
        print(f"\n[PIPELINE] FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()

        # Attempt to complete run log with failure status
        try:
            repo.complete_run_log(
                run_id=  run_id,
                result=  _empty_result(run_id),
                runtime= runtime,
                status=  "FAILED",
                error=   str(e),
            )
        except Exception:
            pass

        return 1


# -----------------------------------------------------------------------------
# EXCEPTION ALERTER
# Sends alerts for new and recurring exceptions based on priority and SLA
# -----------------------------------------------------------------------------

def _send_exception_alerts(result, run_id: str, repo: Repository) -> None:
    """
    Sends exception alerts to entity accountants and escalates as needed.

    Alert logic:
    - NEW exceptions: alert both sides immediately
    - RECURRING exceptions: check SLA — escalate if threshold exceeded
    - P1 exceptions: always alert regardless of age
    - Config staleness: route to CFO if failsafe mode active
    """
    try:
        from src.notification.notifier import Notifier
        notifier = Notifier(run_id=run_id, repo=repo)
        notifier.send_exception_alerts(result.exceptions)
        notifier.check_je_sla_alerts()
        notifier.check_config_staleness()
    except ImportError:
        print(
            "[PIPELINE] Notifier not yet available. "
            "Exception alerts will be sent once notifier.py is complete."
        )
    except Exception as e:
        print(f"[PIPELINE] Alert sending failed: {e}")
        print("[PIPELINE] Pipeline continues — alert failure is non-fatal.")


# -----------------------------------------------------------------------------
# EMPTY RESULT (for failed run log entries)
# -----------------------------------------------------------------------------

def _empty_result(run_id: str):
    """Creates a minimal MatchResult for failed run log entries."""
    from src.matching.engine import MatchResult
    return MatchResult(
        run_id=run_id, period="Unknown",
        started_at=datetime.utcnow(), completed_at=datetime.utcnow(),
        total_ic_rows=0, tier1_matches=0, tier2_matches=0,
        tier3_matches=0, tier4_matches=0, llm_suggestions=0,
        total_exceptions=0, orphans=0, timing_gaps=0,
        amount_mismatches=0, account_mismatches=0,
        currency_mismatches=0, other_exceptions=0,
        stp_rate=0.0, stp_previous_run=0.0,
        stp_delta=0.0, stp_alert_fired=False,
    )


# -----------------------------------------------------------------------------
# ENTRY POINT
# Handles both GitHub Actions environment and local CLI usage
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="R2R APAC Intercompany Reconciliation Pipeline"
    )
    parser.add_argument(
        "--file", "-f",
        type=str,
        default=None,
        help="Path to NetSuite GL CSV file. If omitted, polls Gmail inbox.",
    )
    parser.add_argument(
        "--period", "-p",
        type=str,
        default=None,
        help="Period override e.g. '2026-06'. Auto-detected from CSV if omitted.",
    )
    parser.add_argument(
        "--reason", "-r",
        type=str,
        default=os.environ.get("MANUAL_TRIGGER_REASON", ""),
        help="Reason for manual trigger (SOX audit log entry).",
    )

    args = parser.parse_args()

    # Entity filter from GitHub Actions workflow_dispatch
    entity_filter = os.environ.get("RECON_TARGET", "ALL_ENTITIES_APAC")

    exit_code = run_pipeline(
        csv_file_path= args.file,
        manual_reason= args.reason or None,
        entity_filter= entity_filter,
    )

    sys.exit(exit_code)
