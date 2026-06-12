# =============================================================================
# R2R INTERCOMPANY RECONCILIATION ENGINE
# src/notification/notifier.py
#
# What this file does:
#   Sends all automated alerts and escalations.
#   Three types of notifications:
#
#   1. Exception alerts:
#      When a new IC mismatch is detected, both counterparty entity
#      accountants are notified simultaneously. Not just one side.
#      The email explains what is unmatched, which entity needs to act,
#      and what the deadline is.
#
#   2. JE posting SLA alerts:
#      When an approved JE draft has not been posted to NetSuite
#      within the SLA window, escalating alerts fire:
#        0-24hrs:  No alert
#        24-48hrs: Primary controller notified
#        48-72hrs: Primary + backup controller notified
#        >72hrs:   Head of Accounting + CFO notified
#
#   3. Config staleness alerts:
#      When config.yaml has not been verified in 75+ days,
#      reminder emails fire. At 104+ days, fail-safe routing
#      redirects all alerts to the CFO only.
#
#   Notification channels:
#     Primary:  Gmail API (google-auth OAuth 3LO)
#     Fallback: Telegram Bot API (bypasses corporate spam filters)
#
#   Privacy:
#     No financial amounts in email subjects (amount buckets only).
#     Entity names and account codes in body — controlled distribution.
#     Telegram fallback: plain text summary only, no amounts.
#
# How other files use it:
#   from src.notification.notifier import Notifier
#   notifier = Notifier(run_id=run_id, repo=repo)
#   notifier.send_exception_alerts(exceptions_df)
#   notifier.check_je_sla_alerts()
#   notifier.check_config_staleness()
# =============================================================================

import os
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import pandas as pd
import requests

from src.config import config, get_escalation_recipient, is_failsafe_mode
from src.persistence.repository import Repository


# -----------------------------------------------------------------------------
# AMOUNT BUCKETER
# Never puts exact amounts in notification subjects
# Body contains amounts — subject uses buckets only
# -----------------------------------------------------------------------------

def _amount_bucket(amount_usd: float) -> str:
    """
    Converts an exact amount to a materiality bucket for email subjects.
    Protects exact financial data from appearing in email subject lines
    which may be logged by corporate email servers.

    Examples:
        650000  → ">$500K"
        250000  → "$100K-$500K"
        45000   → "$10K-$100K"
        500     → "<$10K"
    """
    abs_amount = abs(float(amount_usd or 0))
    if abs_amount >= 500000:  return ">$500K"
    if abs_amount >= 100000:  return "$100K-$500K"
    if abs_amount >= 10000:   return "$10K-$100K"
    return "<$10K"


# -----------------------------------------------------------------------------
# WORKING DAYS CALCULATOR
# Escalation uses working days, not calendar hours
# -----------------------------------------------------------------------------

def _working_days_elapsed(from_date: datetime) -> int:
    """
    Calculates the number of working days elapsed since from_date.
    Monday-Friday only. Does not account for public holidays
    (acceptable for prototype — add holiday calendar in V2).
    """
    if from_date is None:
        return 0

    now   = datetime.utcnow()
    delta = now - from_date
    days  = delta.days

    # Count only Mon-Fri
    working_days = 0
    current = from_date
    for _ in range(days):
        if current.weekday() < 5:  # 0=Monday, 4=Friday
            working_days += 1
        current += timedelta(days=1)

    return working_days


# -----------------------------------------------------------------------------
# GMAIL SENDER
# Uses Gmail API with OAuth 3LO refresh token
# Falls back to SMTP if API fails
# -----------------------------------------------------------------------------

class GmailSender:
    """
    Sends emails via Gmail API using OAuth 3LO credentials.

    The refresh token is stored in GitHub Secrets as GMAIL_REFRESH_TOKEN.
    The GCP OAuth consent screen must be set to Production to prevent
    the 7-day token expiry that affects Testing mode projects.
    """

    def __init__(self):
        self.refresh_token  = os.environ.get("GMAIL_REFRESH_TOKEN", "")
        self.client_id      = os.environ.get("GMAIL_CLIENT_ID", "")
        self.client_secret  = os.environ.get("GMAIL_CLIENT_SECRET", "")
        self.sender_email   = config.notifications.gmail.__dict__.get(
            "sender", "recon-pipeline@gmail.com"
        )
        self._service       = None

    def _get_service(self):
        """Initialises Gmail API service with OAuth credentials."""
        if self._service:
            return self._service

        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            creds = Credentials(
                token=         None,
                refresh_token= self.refresh_token,
                client_id=     self.client_id,
                client_secret= self.client_secret,
                token_uri=     "https://oauth2.googleapis.com/token",
                scopes=        ["https://www.googleapis.com/auth/gmail.send"],
            )
            self._service = build("gmail", "v1", credentials=creds)
            return self._service

        except Exception as e:
            print(f"[NOTIFIER] Gmail API init failed: {e}")
            return None

    def send(
        self,
        to:      str,
        subject: str,
        body:    str,
    ) -> bool:
        """
        Sends an email via Gmail API.
        Falls back to a print statement if API unavailable.

        Returns True if sent successfully.
        """
        if not self.refresh_token:
            print(
                f"[NOTIFIER] Gmail not configured. Would send to: {to}\n"
                f"  Subject: {subject}\n"
                f"  Body preview: {body[:200]}..."
            )
            return False

        try:
            import base64
            service = self._get_service()
            if not service:
                return self._smtp_fallback(to, subject, body)

            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = self.sender_email
            msg["To"]      = to
            msg.attach(MIMEText(body, "plain"))

            raw = base64.urlsafe_b64encode(
                msg.as_bytes()
            ).decode("utf-8")

            service.users().messages().send(
                userId="me",
                body={"raw": raw},
            ).execute()

            print(f"[NOTIFIER] Email sent to: {to}")
            return True

        except Exception as e:
            print(f"[NOTIFIER] Gmail API send failed: {e}. Trying SMTP fallback.")
            return self._smtp_fallback(to, subject, body)

    def _smtp_fallback(self, to: str, subject: str, body: str) -> bool:
        """SMTP fallback — used when Gmail API is unavailable."""
        try:
            smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
            smtp_port = int(os.environ.get("SMTP_PORT", "587"))
            smtp_user = os.environ.get("SMTP_USER", "")
            smtp_pass = os.environ.get("SMTP_PASSWORD", "")

            if not smtp_user:
                print(f"[NOTIFIER] SMTP not configured. Email to {to} not sent.")
                return False

            msg = MIMEText(body, "plain")
            msg["Subject"] = subject
            msg["From"]    = smtp_user
            msg["To"]      = to

            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)

            print(f"[NOTIFIER] SMTP fallback: email sent to {to}")
            return True

        except Exception as e:
            print(f"[NOTIFIER] SMTP fallback failed: {e}")
            return False


# -----------------------------------------------------------------------------
# TELEGRAM SENDER
# Fallback for corporate spam filter bypass
# Sends to entity accountant Telegram chat IDs in config.yaml
# -----------------------------------------------------------------------------

class TelegramSender:
    """
    Sends notifications via Telegram Bot API.
    Used when Gmail alerts are filtered by corporate spam.
    Requires entity accountants to have Telegram installed
    and to have started a chat with the bot.
    """

    def __init__(self):
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.enabled   = bool(
            self.bot_token and
            config.notifications.__dict__.get("telegram", {})
            .get("enabled", False)
        )

    def send(self, chat_id: str, message: str) -> bool:
        """
        Sends a Telegram message to a specific chat ID.
        Returns True if sent successfully.
        """
        if not self.enabled or not chat_id:
            return False

        try:
            url  = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            resp = requests.post(url, json={
                "chat_id":    chat_id,
                "text":       message,
                "parse_mode": "Markdown",
            }, timeout=10)
            resp.raise_for_status()
            print(f"[NOTIFIER] Telegram sent to chat_id: {chat_id}")
            return True

        except Exception as e:
            print(f"[NOTIFIER] Telegram send failed: {e}")
            return False


# -----------------------------------------------------------------------------
# NOTIFIER CLASS
# Main notification engine — orchestrates all alert types
# -----------------------------------------------------------------------------

class Notifier:
    """
    Central notification engine.

    Usage:
        notifier = Notifier(run_id=run_id, repo=repo)
        notifier.send_exception_alerts(exceptions_df)
        notifier.check_je_sla_alerts()
        notifier.check_config_staleness()
    """

    def __init__(self, run_id: str, repo: Repository):
        self.run_id   = run_id
        self.repo     = repo
        self.gmail    = GmailSender()
        self.telegram = TelegramSender()
        self.failsafe = is_failsafe_mode()

        if self.failsafe:
            print(
                "[NOTIFIER] FAIL-SAFE MODE ACTIVE: "
                "All alerts will be routed to CFO only. "
                "Update last_verified_date in config.yaml to restore "
                "normal routing."
            )

    # ------------------------------------------------------------------
    # EXCEPTION ALERTS
    # ------------------------------------------------------------------

    def send_exception_alerts(self, exceptions_df: pd.DataFrame) -> None:
        """
        Sends alerts for new and recurring exceptions.

        Both counterparty entities are notified — not just the source.
        This is critical: either side can post the missing entry.
        """
        if exceptions_df is None or len(exceptions_df) == 0:
            print("[NOTIFIER] No exceptions to alert.")
            return

        alerted = 0
        for _, exc in exceptions_df.iterrows():
            period_status = str(exc.get("period_status", "NEW"))
            priority      = str(exc.get("priority", "P3"))

            # Alert criteria:
            # NEW exceptions: always alert
            # RECURRING exceptions: alert if P1 or working days exceeded SLA
            should_alert = False
            if period_status == "NEW":
                should_alert = True
            elif period_status == "RECURRING":
                # P1 always gets an alert
                if priority == "P1":
                    should_alert = True
                # Others: check working days vs SLA
                else:
                    occurrence = int(exc.get("occurrence_count", 1) or 1)
                    tier1_days = config.escalation.exception_tier_1_days
                    if occurrence > tier1_days:
                        should_alert = True

            if not should_alert:
                continue

            self._send_exception_alert(exc)
            alerted += 1

        print(f"[NOTIFIER] Exception alerts sent: {alerted}")

    def _send_exception_alert(self, exc: pd.Series) -> None:
        """
        Sends an exception alert to both counterparty entities.
        Routes through fail-safe if config is stale.
        """
        source_entity  = str(exc.get("source_entity", ""))
        target_entity  = str(exc.get("target_cseg", ""))
        exception_type = str(exc.get("exception_type", "ORPHAN"))
        priority       = str(exc.get("priority", "P3"))
        amount         = exc.get("unmatched_fxamount", 0)
        currency       = str(exc.get("unmatched_currency", ""))
        trandate       = str(exc.get("trandate", ""))
        account_name   = str(exc.get("account_name", ""))
        llm_suggested  = str(exc.get("llm_suggested_entity", "") or "")
        occurrence     = int(exc.get("occurrence_count", 1) or 1)
        period         = str(exc.get("period", ""))
        exception_id   = str(exc.get("exception_id", ""))

        amount_bucket = _amount_bucket(amount)
        subject = (
            f"[{priority}] IC Exception: {exception_type} | "
            f"{source_entity} ↔ {target_entity} | "
            f"{amount_bucket} | {period}"
        )

        # Build email body
        recurring_note = (
            f"\n⚠  This exception has been open for {occurrence} "
            f"consecutive runs. Immediate action required.\n"
            if occurrence > 1 else ""
        )

        llm_note = (
            f"\nAI Suggestion: The most likely counterparty is "
            f"{llm_suggested}. Please verify.\n"
            if llm_suggested else ""
        )

        body = f"""APAC Intercompany Reconciliation — Exception Alert
{'='*60}

Period:          {period}
Exception Type:  {exception_type}
Priority:        {priority}
{recurring_note}
Source Entity:   {source_entity}
Target Entity:   {target_entity}
Transaction Date:{trandate}
Account:         {account_name}
Currency:        {currency}
Amount:          {amount:,.2f} {currency}
{llm_note}
ACTION REQUIRED:
{'─'*40}
Please ensure the corresponding intercompany entry is posted
in NetSuite by the end of the next working day.

If this entry has already been posted, reply to this email
with the NetSuite internal ID so we can investigate.

Exception ID: {exception_id}
Run ID: {self.run_id}

This is an automated alert from the R2R Reconciliation Engine.
Do not reply to this email with financial data.
Contact the Head of Accounting for queries.
"""

        # Determine recipients
        if self.failsafe:
            # Fail-safe: route to CFO only
            recipients = [config.escalation.cfo_email]
        else:
            recipients = []
            # Source entity controller
            src_config = config.entities.get(source_entity)
            if src_config:
                recipients.append(src_config.primary_controller)
            # Target entity controller (both sides notified)
            tgt_config = config.entities.get(target_entity)
            if tgt_config:
                recipients.append(tgt_config.primary_controller)
            # P1: also notify Head of Accounting directly
            if priority == "P1":
                recipients.append(config.escalation.head_of_accounting_email)

        # Remove duplicates and empty strings
        recipients = list(set(r for r in recipients if r and r.strip()))

        for recipient in recipients:
            sent = self.gmail.send(recipient, subject, body)

            # Telegram fallback if email failed
            if not sent:
                entity_code = (
                    source_entity if source_entity in config.entities
                    else target_entity
                )
                entity_cfg = config.entities.get(entity_code)
                if entity_cfg and entity_cfg.telegram_chat_id:
                    self.telegram.send(
                        chat_id= entity_cfg.telegram_chat_id,
                        message= (
                            f"*IC Exception Alert* [{priority}]\n"
                            f"{exception_type} | {source_entity} ↔ "
                            f"{target_entity}\n"
                            f"Amount: {amount_bucket} {currency}\n"
                            f"Period: {period}\n"
                            f"Check email for details."
                        ),
                    )

            # Log notification
            self.repo.log_notification(
                run_id=            self.run_id,
                notification_type= f"EXCEPTION_ALERT_{priority}",
                channel=           "EMAIL",
                recipient=         recipient,
                subject=           subject,
                exception_id=      exception_id,
            )

    # ------------------------------------------------------------------
    # JE POSTING SLA ALERTS
    # ------------------------------------------------------------------

    def check_je_sla_alerts(self) -> None:
        """
        Checks for approved JE drafts that have not been posted.
        Sends escalating alerts based on hours elapsed since approval.

        SLA tiers (from config.yaml):
            0-24hrs:  No alert
            24-48hrs: Warning to primary controller
            48-72hrs: Escalation to primary + backup
            >72hrs:   Critical to Head of Accounting + CFO
        """
        unposted_df = self.repo.get_unposted_je_drafts(
            hours_threshold=config.escalation.je_posting_sla_warning_hours
        )

        if unposted_df is None or len(unposted_df) == 0:
            print("[NOTIFIER] No unposted JE SLA breaches.")
            return

        alerted = 0
        for _, draft in unposted_df.iterrows():
            hours_unposted = float(draft.get("hours_unposted", 0) or 0)
            subsidiary     = str(draft.get("subsidiary", ""))
            je_type        = str(draft.get("je_type", ""))
            je_draft_id    = str(draft.get("je_draft_id", ""))
            period         = str(draft.get("period", ""))

            # Determine severity and recipients
            esc = config.escalation
            if hours_unposted > esc.je_posting_sla_critical_hours:
                severity   = "CRITICAL"
                recipients = [
                    config.escalation.head_of_accounting_email,
                    config.escalation.cfo_email,
                ]
            elif hours_unposted > esc.je_posting_sla_escalation_hours:
                severity   = "ESCALATION"
                entity_cfg = config.entities.get(subsidiary)
                recipients = [
                    entity_cfg.primary_controller if entity_cfg else "",
                    entity_cfg.backup_controller  if entity_cfg else "",
                ]
            else:
                severity   = "WARNING"
                entity_cfg = config.entities.get(subsidiary)
                recipients = [
                    entity_cfg.primary_controller if entity_cfg else ""
                ]

            if self.failsafe:
                recipients = [config.escalation.cfo_email]

            recipients = list(set(r for r in recipients if r and r.strip()))

            subject = (
                f"[{severity}] Unposted JE Draft: {je_draft_id} | "
                f"{subsidiary} | {period} | "
                f"{hours_unposted:.0f}hrs overdue"
            )

            body = f"""APAC Intercompany Reconciliation — Unposted JE Alert
{'='*60}

Severity:        {severity}
JE Draft ID:     {je_draft_id}
JE Type:         {je_type}
Subsidiary:      {subsidiary}
Period:          {period}
Hours Overdue:   {hours_unposted:.1f} hours

This approved journal entry draft has NOT been posted to NetSuite.

ACTION REQUIRED:
{'─'*40}
1. Open the JE Drafts tab in the reconciliation Excel report
2. Locate JE Draft ID: {je_draft_id}
3. Review and post to NetSuite immediately
4. Update the posted_to_netsuite column

Failure to post approved JE drafts is a SOX ITGC control breach.

Run ID: {self.run_id}
"""

            for recipient in recipients:
                self.gmail.send(recipient, subject, body)
                self.repo.log_notification(
                    run_id=            self.run_id,
                    notification_type= f"JE_REMINDER_{severity}",
                    channel=           "EMAIL",
                    recipient=         recipient,
                    subject=           subject,
                    je_draft_id=       je_draft_id,
                )
                alerted += 1

        print(f"[NOTIFIER] JE SLA alerts sent: {alerted}")

    # ------------------------------------------------------------------
    # CONFIG STALENESS ALERT
    # ------------------------------------------------------------------

    def check_config_staleness(self) -> None:
        """
        Checks config.yaml verification age and sends alerts if stale.
        Mirrors the governance check in config.py but sends emails.
        """
        gov       = config.governance
        age_days  = (datetime.utcnow() - gov.last_verified_date).days

        if age_days <= gov.reminder_days:
            return  # All good

        if age_days <= gov.warning_days:
            severity = "REMINDER"
            message  = (
                f"config.yaml has not been verified for {age_days} days. "
                f"Please review all contact emails and update "
                f"last_verified_date. "
                f"Warning threshold: {gov.warning_days} days. "
                f"Fail-safe activates at: {gov.failsafe_days} days."
            )
            recipients = [gov.system_owner_email]

        elif age_days <= gov.failsafe_days:
            severity = "WARNING"
            message  = (
                f"config.yaml is {age_days} days old. "
                f"Escalation contacts may be stale. "
                f"Fail-safe routing will activate in "
                f"{gov.failsafe_days - age_days} days. "
                f"Update last_verified_date immediately."
            )
            recipients = [gov.system_owner_email, gov.executive_sponsor_email]

        else:
            severity = "FAIL_SAFE_ACTIVE"
            message  = (
                f"config.yaml is {age_days} days old. "
                f"FAIL-SAFE ROUTING IS NOW ACTIVE. "
                f"All exception alerts are being routed to the CFO only. "
                f"Normal routing will resume when last_verified_date is updated."
            )
            recipients = [gov.executive_sponsor_email]

        subject = f"[{severity}] R2R Config Review Required — {age_days} days old"
        body = f"""APAC R2R Reconciliation Engine — Config Staleness Alert
{'='*60}

Severity:     {severity}
Days Since Verification: {age_days} days
Last Verified: {gov.last_verified_date.strftime('%Y-%m-%d')}

{message}

HOW TO RESOLVE:
{'─'*40}
1. Open config.yaml in the r2r-reconciliation GitHub repository
2. Review all entity contact email addresses
3. Confirm escalation contacts are current
4. Update last_verified_date to today's date: {datetime.utcnow().strftime('%Y-%m-%d')}
5. Create a Pull Request with your changes
6. Get PR approved by the designated reviewer
7. Merge — the pipeline will detect the updated date automatically

Run ID: {self.run_id}
"""

        for recipient in recipients:
            self.gmail.send(recipient, subject, body)
            self.repo.log_notification(
                run_id=            self.run_id,
                notification_type= f"CONFIG_STALENESS_{severity}",
                channel=           "EMAIL",
                recipient=         recipient,
                subject=           subject,
            )

        print(f"[NOTIFIER] Config staleness alert sent: {severity} ({age_days} days)")

    # ------------------------------------------------------------------
    # STP ALERT
    # ------------------------------------------------------------------

    def send_stp_alert(self, stp_rate: float, stp_previous: float) -> None:
        """
        Sends a high-severity alert when STP rate drops materially.
        Called by engine.py when stp_alert_fired = True.
        """
        drop_pct = stp_previous - stp_rate

        subject = (
            f"[HIGH] STP Rate Alert: Dropped {drop_pct:.1%} | "
            f"Current: {stp_rate:.1%}"
        )

        body = f"""APAC R2R Reconciliation Engine — STP Rate Alert
{'='*60}

ALERT: Straight-Through Processing rate has dropped materially.

Current STP:   {stp_rate:.1%}
Previous STP:  {stp_previous:.1%}
Drop:          {drop_pct:.1%}
Threshold:     {config.governance.stp_alert_drop_pct}%

POSSIBLE CAUSES:
{'─'*40}
1. NetSuite Saved Search column structure changed
2. New entity or account code not in config.yaml
3. Unusual volume of manual journal entries this period
4. CSEG field renamed or removed by NetSuite admin

ACTION REQUIRED:
Review the Exceptions tab in the reconciliation report.
Check the validator.py output in GitHub Actions logs.

Run ID: {self.run_id}
"""

        recipients = [
            config.escalation.head_of_accounting_email,
        ]
        if self.failsafe:
            recipients = [config.escalation.cfo_email]

        for recipient in recipients:
            self.gmail.send(recipient, subject, body)

        print(f"[NOTIFIER] STP alert sent. Drop: {drop_pct:.1%}")