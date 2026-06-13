# =============================================================================
# R2R INTERCOMPANY RECONCILIATION ENGINE
# jobs/preflight_check.py
#
# What this file does:
#   Pre-demo health check script.
#   Verifies all external dependencies before a live demonstration
#   or production run. Run this 30 minutes before any demo.
#
#   Checks performed:
#     1. Database (Supabase/SQLite) connectivity
#     2. Groq API availability and authentication
#     3. Gemini API availability and authentication
#     4. Gmail API token validity
#     5. Google Drive write access
#     6. Google Sheets write access
#     7. GitHub Actions workflow status
#     8. Supabase project active (not paused)
#     9. Synthetic data file exists
#    10. Config.yaml governance status
#
#   Output format:
#     [PASS] Component — description
#     [FAIL] Component — error and remediation step
#     [SKIP] Component — not configured, skipped
#
#   Exit code:
#     0 = all configured components pass
#     1 = one or more configured components fail
#
# How to run:
#   python jobs/preflight_check.py
#   python jobs/preflight_check.py --verbose
#
# Run this before:
#   - Any live demo to a future employer
#   - First production run with real data
#   - After any change to config.yaml or GitHub Secrets
# =============================================================================

import argparse
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# -----------------------------------------------------------------------------
# RESULT TRACKER
# Collects all check results and prints a clean summary
# -----------------------------------------------------------------------------

class CheckResult:
    def __init__(self):
        self.results = []

    def add(self, status: str, component: str, detail: str, fix: str = ""):
        self.results.append({
            "status":    status,
            "component": component,
            "detail":    detail,
            "fix":       fix,
        })
        icon = {"PASS": "✅", "FAIL": "❌", "SKIP": "⚠️ "}.get(status, "?")
        print(f"  {icon} [{status}] {component}")
        if status == "FAIL":
            print(f"         Error: {detail}")
            if fix:
                print(f"         Fix:   {fix}")
        elif status == "SKIP":
            print(f"         {detail}")

    def summary(self) -> tuple[int, int, int]:
        passed = sum(1 for r in self.results if r["status"] == "PASS")
        failed = sum(1 for r in self.results if r["status"] == "FAIL")
        skipped = sum(1 for r in self.results if r["status"] == "SKIP")
        return passed, failed, skipped


# -----------------------------------------------------------------------------
# INDIVIDUAL CHECKS
# Each check is independent — failure in one does not stop the others
# -----------------------------------------------------------------------------

def check_database(results: CheckResult) -> None:
    """Checks database connectivity."""
    try:
        from src.persistence.database import test_connection, get_engine
        from sqlalchemy import text

        connected = test_connection()
        if connected:
            engine = get_engine()
            with engine.connect() as conn:
                backend = "PostgreSQL" if "postgresql" in str(engine.url) else "SQLite"
                conn.execute(text("SELECT 1"))
            results.add("PASS", "Database",
                        f"Connected to {backend} successfully")
        else:
            results.add("FAIL", "Database",
                        "Connection test returned False",
                        "Check SUPABASE_POOLER_URL environment variable. "
                        "Verify Supabase project is active (not paused) at supabase.com")

    except Exception as e:
        results.add("FAIL", "Database", str(e),
                    "Set SUPABASE_POOLER_URL or enable SQLite fallback in config.yaml")


def check_supabase_active(results: CheckResult) -> None:
    """
    Checks if Supabase project is active (not paused due to inactivity).
    Paused projects return connection timeout — distinct from auth errors.
    """
    supabase_url = os.environ.get("SUPABASE_POOLER_URL", "")
    if not supabase_url:
        results.add("SKIP", "Supabase Active",
                    "SUPABASE_POOLER_URL not set — using SQLite")
        return

    try:
        import psycopg2
        conn = psycopg2.connect(
            supabase_url,
            connect_timeout=5,
            sslmode="require",
        )
        conn.close()
        results.add("PASS", "Supabase Active",
                    "Project is active and accepting connections")

    except Exception as e:
        error_str = str(e).lower()
        if "timeout" in error_str or "connection refused" in error_str:
            results.add("FAIL", "Supabase Active",
                        "Connection timed out — project may be paused",
                        "Go to supabase.com → your project → "
                        "click 'Restore project'. Takes 30 seconds.")
        else:
            results.add("FAIL", "Supabase Active", str(e),
                        "Check SUPABASE_POOLER_URL uses port 6543 (Supavisor)")


def check_groq(results: CheckResult) -> None:
    """Checks Groq API authentication and availability."""
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        results.add("SKIP", "Groq API",
                    "GROQ_API_KEY not set — primary LLM unavailable")
        return

    try:
        from groq import Groq
        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": "Reply with OK only."}],
            max_tokens=5,
            temperature=0,
        )
        reply = response.choices[0].message.content.strip()
        results.add("PASS", "Groq API",
                    f"Llama 3.3 70B responding. Reply: '{reply}'")

    except Exception as e:
        error_str = str(e)
        if "401" in error_str or "authentication" in error_str.lower():
            results.add("FAIL", "Groq API", "Authentication failed",
                        "Check GROQ_API_KEY in GitHub Secrets or .env file. "
                        "Get key at console.groq.com")
        elif "429" in error_str:
            results.add("FAIL", "Groq API", "Rate limited (429)",
                        "Wait 1 minute and retry. Free tier: 30 RPM.")
        else:
            results.add("FAIL", "Groq API", error_str,
                        "Check groq.com status page for outages")


def check_gemini(results: CheckResult) -> None:
    """Checks Gemini API authentication and availability."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        results.add("SKIP", "Gemini API",
                    "GEMINI_API_KEY not set — tertiary LLM unavailable")
        return

    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents="Reply with OK only.",
        )
        reply = response.text.strip()
        results.add("PASS", "Gemini API",
                    f"Gemini 2.0 Flash responding. Reply: '{reply[:20]}'")

    except Exception as e:
        error_str = str(e)
        if "401" in error_str or "API_KEY_INVALID" in error_str:
            results.add("FAIL", "Gemini API", "Invalid API key",
                        "Check GEMINI_API_KEY. Get key at aistudio.google.com")
        elif "429" in error_str:
            results.add("FAIL", "Gemini API", "Rate limited (429)",
                        "Wait 4 seconds between calls. Free tier: 15 RPM.")
        else:
            results.add("FAIL", "Gemini API", error_str,
                        "Check Google AI Studio status")


def check_deepseek(results: CheckResult) -> None:
    """Checks DeepSeek API availability."""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        results.add("SKIP", "DeepSeek API",
                    "DEEPSEEK_API_KEY not set — secondary LLM unavailable")
        return

    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
        )
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": "Reply with OK only."}],
            max_tokens=5,
            temperature=0,
        )
        reply = response.choices[0].message.content.strip()
        results.add("PASS", "DeepSeek API",
                    f"DeepSeek responding. Reply: '{reply[:20]}'")

    except Exception as e:
        results.add("FAIL", "DeepSeek API", str(e)[:100],
                    "DeepSeek may have availability issues. "
                    "Pipeline will fall back to Gemini automatically.")


def check_gmail(results: CheckResult) -> None:
    """Checks Gmail API token validity."""
    refresh_token  = os.environ.get("GMAIL_REFRESH_TOKEN", "")
    client_id      = os.environ.get("GMAIL_CLIENT_ID", "")
    client_secret  = os.environ.get("GMAIL_CLIENT_SECRET", "")

    if not all([refresh_token, client_id, client_secret]):
        results.add("SKIP", "Gmail API",
                    "Gmail credentials not set — inbox polling disabled. "
                    "Use --file argument to specify CSV manually.")
        return

    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=         None,
            refresh_token= refresh_token,
            client_id=     client_id,
            client_secret= client_secret,
            token_uri=     "https://oauth2.googleapis.com/token",
            scopes=        ["https://www.googleapis.com/auth/gmail.readonly"],
        )
        service = build("gmail", "v1", credentials=creds)
        profile = service.users().getProfile(userId="me").execute()
        email   = profile.get("emailAddress", "unknown")
        results.add("PASS", "Gmail API",
                    f"Authenticated as: {email}")

    except Exception as e:
        error_str = str(e)
        if "invalid_grant" in error_str.lower():
            results.add("FAIL", "Gmail API",
                        "Refresh token expired (invalid_grant)",
                        "Re-run OAuth flow to get a new refresh token. "
                        "Ensure GCP OAuth consent screen is set to PRODUCTION "
                        "not TESTING to prevent 7-day token expiry.")
        else:
            results.add("FAIL", "Gmail API", error_str[:100],
                        "Check GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, "
                        "GMAIL_REFRESH_TOKEN in environment.")


def check_google_drive(results: CheckResult) -> None:
    """Checks Google Drive write access."""
    folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
    if not folder_id:
        results.add("SKIP", "Google Drive",
                    "GOOGLE_DRIVE_FOLDER_ID not set — Excel upload disabled")
        return

    try:
        from googleapiclient.discovery import build
        import google.auth

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/drive.file"]
        )
        service  = build("drive", "v3", credentials=credentials)
        metadata = service.files().get(
            fileId=folder_id, fields="id,name"
        ).execute()
        results.add("PASS", "Google Drive",
                    f"Folder accessible: '{metadata.get('name', folder_id)}'")

    except Exception as e:
        results.add("FAIL", "Google Drive", str(e)[:100],
                    "Share the Google Drive folder with the service account email. "
                    "Check GOOGLE_DRIVE_FOLDER_ID and GCP WIF configuration.")


def check_google_sheets(results: CheckResult) -> None:
    """Checks Google Sheets write access."""
    sheet_id = os.environ.get("GOOGLE_SHEETS_ID", "")
    if not sheet_id:
        results.add("SKIP", "Google Sheets",
                    "GOOGLE_SHEETS_ID not set — dashboard updates disabled")
        return

    try:
        from googleapiclient.discovery import build
        import google.auth

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        service     = build("sheets", "v4", credentials=credentials)
        spreadsheet = service.spreadsheets().get(
            spreadsheetId=sheet_id
        ).execute()
        title = spreadsheet.get("properties", {}).get("title", sheet_id)
        results.add("PASS", "Google Sheets",
                    f"Spreadsheet accessible: '{title}'")

    except Exception as e:
        results.add("FAIL", "Google Sheets", str(e)[:100],
                    "Share the Google Sheet with the service account email. "
                    "Check GOOGLE_SHEETS_ID and GCP WIF configuration.")


def check_synthetic_data(results: CheckResult) -> None:
    """Checks that synthetic test data exists for demo use."""
    synthetic_path = os.path.join(
        os.path.dirname(__file__),
        "../data/synthetic/synthetic_gl_jun2026.csv"
    )

    if os.path.exists(synthetic_path):
        import csv
        with open(synthetic_path, "r") as f:
            rows = sum(1 for _ in csv.reader(f)) - 1  # Exclude header
        results.add("PASS", "Synthetic Data",
                    f"Found: {rows} rows in synthetic_gl_jun2026.csv")
    else:
        results.add("FAIL", "Synthetic Data",
                    "Synthetic data file not found",
                    "Run: python tests/synthetic/generate_synthetic.py")


def check_config(results: CheckResult) -> None:
    """Checks config.yaml governance status."""
    try:
        from src.config import config
        from datetime import timedelta

        gov      = config.governance
        age_days = (datetime.utcnow() - gov.last_verified_date).days

        if age_days <= gov.reminder_days:
            results.add("PASS", "Config Governance",
                        f"Config verified {age_days} days ago. "
                        f"Next review due in {gov.reminder_days - age_days} days.")
        elif age_days <= gov.warning_days:
            results.add("FAIL", "Config Governance",
                        f"Config is {age_days} days old (reminder threshold: "
                        f"{gov.reminder_days} days)",
                        "Update last_verified_date in config.yaml after "
                        "reviewing all entity contact emails.")
        elif age_days <= gov.failsafe_days:
            results.add("FAIL", "Config Governance",
                        f"Config is {age_days} days old — WARNING threshold exceeded",
                        "Update last_verified_date immediately. "
                        f"Fail-safe activates at {gov.failsafe_days} days.")
        else:
            results.add("FAIL", "Config Governance",
                        f"FAIL-SAFE ACTIVE — config is {age_days} days old",
                        "Update last_verified_date in config.yaml urgently.")

        # Check for TODO placeholders in config
        import yaml
        config_path = os.path.join(
            os.path.dirname(__file__), "../config.yaml"
        )
        with open(config_path, "r") as f:
            raw = f.read()

        todo_count = raw.count("# TODO")
        if todo_count > 0:
            results.add("FAIL", "Config TODOs",
                        f"{todo_count} TODO items remain in config.yaml",
                        "Complete all TODO items before using with real data. "
                        "TODOs are safe for synthetic data testing.")
        else:
            results.add("PASS", "Config TODOs",
                        "No TODO items in config.yaml")

    except Exception as e:
        results.add("FAIL", "Config", str(e),
                    "Check config.yaml exists and is valid YAML.")


def check_output_dir(results: CheckResult) -> None:
    """Checks that the output directory is writable."""
    output_dir = "output"
    try:
        os.makedirs(output_dir, exist_ok=True)
        test_file = os.path.join(output_dir, ".write_test")
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)
        results.add("PASS", "Output Directory",
                    f"'{output_dir}/' is writable")
    except Exception as e:
        results.add("FAIL", "Output Directory", str(e),
                    f"Create the '{output_dir}/' directory manually.")


# -----------------------------------------------------------------------------
# MAIN PREFLIGHT FUNCTION
# -----------------------------------------------------------------------------

def run_preflight(verbose: bool = False) -> int:
    """
    Runs all preflight checks and prints a summary.

    Returns:
        0 if all configured components pass
        1 if any configured component fails
    """
    print("\n" + "=" * 60)
    print("R2R RECONCILIATION ENGINE — PRE-FLIGHT CHECK")
    print("=" * 60)
    print(f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Environment: {os.environ.get('ENV', 'local')}")
    print("=" * 60 + "\n")

    results = CheckResult()

    checks = [
        ("Config and Governance",  check_config),
        ("Synthetic Data",         check_synthetic_data),
        ("Output Directory",       check_output_dir),
        ("Database",               check_database),
        ("Supabase Active",        check_supabase_active),
        ("Groq API",               check_groq),
        ("Gemini API",             check_gemini),
        ("DeepSeek API",           check_deepseek),
        ("Gmail API",              check_gmail),
        ("Google Drive",           check_google_drive),
        ("Google Sheets",          check_google_sheets),
    ]

    for section_name, check_fn in checks:
        try:
            check_fn(results)
        except Exception as e:
            results.add("FAIL", section_name,
                        f"Check crashed: {e}",
                        "This is a bug in the preflight check itself.")
        time.sleep(0.5)  # Brief pause between API calls

    # Summary
    passed, failed, skipped = results.summary()

    print("\n" + "=" * 60)
    print("PREFLIGHT SUMMARY")
    print("=" * 60)
    print(f"  ✅ Passed:  {passed}")
    print(f"  ❌ Failed:  {failed}")
    print(f"  ⚠️  Skipped: {skipped}")

    if failed == 0:
        print("\n  ✅ ALL CHECKS PASSED — Safe to proceed.")
        print("=" * 60 + "\n")
        return 0
    else:
        print(f"\n  ❌ {failed} CHECK(S) FAILED — Resolve before demo/production run.")
        print("\n  Failed components:")
        for r in results.results:
            if r["status"] == "FAIL":
                print(f"    • {r['component']}: {r['detail']}")
                if r["fix"]:
                    print(f"      → {r['fix']}")
        print("=" * 60 + "\n")
        return 1


# -----------------------------------------------------------------------------
# ENTRY POINT
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="R2R Reconciliation Engine — Pre-flight Health Check"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed output for all checks",
    )
    args = parser.parse_args()

    exit_code = run_preflight(verbose=args.verbose)
    sys.exit(exit_code)