"""
Tool: mailchimp_report_email.py
Purpose: Send the daily Mailchimp report to a configurable list of founder
         emails via SMTP. No addresses are hardcoded — everything comes from
         env vars.

New env vars (added to .env.template, all optional until you enable sending):
  SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_USE_TLS
  REPORT_FROM_EMAIL, REPORT_RECIPIENTS (comma-separated)
  TEST_MODE (default true) — when true, send_report_email() logs what it
  would have sent and returns without opening an SMTP connection.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("mailchimp_report")


def _is_test_mode() -> bool:
    return os.getenv("TEST_MODE", "true").strip().lower() not in ("false", "0", "no")


def get_recipients() -> list[str]:
    raw = os.getenv("REPORT_RECIPIENTS", "")
    return [addr.strip() for addr in raw.split(",") if addr.strip()]


class EmailConfigError(RuntimeError):
    pass


def send_report_email(
    subject: str,
    html_body: str,
    pdf_path: Optional[Path] = None,
    force_send: bool = False,
) -> dict:
    """
    Sends the report email. Returns a status dict for logging/data-quality
    reporting — never raises for delivery failures (send failures are
    reported, not fatal to the overall report run).

    force_send=True still respects TEST_MODE unless TEST_MODE is explicitly
    "false" in .env — this is the deliberate double safety switch so
    production email can't fire by accident.
    """
    recipients = get_recipients()
    from_email = os.getenv("REPORT_FROM_EMAIL")

    if not recipients or not from_email:
        msg = "REPORT_RECIPIENTS and/or REPORT_FROM_EMAIL not configured — skipping email."
        log.warning(msg)
        return {"sent": False, "reason": msg, "recipients": recipients}

    if _is_test_mode():
        msg = f"TEST_MODE is on — would have emailed {len(recipients)} recipient(s): {', '.join(recipients)}"
        log.info(msg)
        return {"sent": False, "reason": "TEST_MODE", "recipients": recipients}

    if not force_send:
        log.info("Email send not requested (force_send=False) — skipping.")
        return {"sent": False, "reason": "not requested", "recipients": recipients}

    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USERNAME")
    smtp_pass = os.getenv("SMTP_PASSWORD")
    use_tls = os.getenv("SMTP_USE_TLS", "true").strip().lower() not in ("false", "0", "no")

    if not smtp_host or not smtp_user or not smtp_pass:
        msg = "SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD not configured — cannot send email."
        log.error(msg)
        return {"sent": False, "reason": msg, "recipients": recipients}

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html"))

    if pdf_path and Path(pdf_path).exists():
        with open(pdf_path, "rb") as f:
            part = MIMEApplication(f.read(), _subtype="pdf")
            part.add_header("Content-Disposition", "attachment", filename=Path(pdf_path).name)
            msg.attach(part)

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            if use_tls:
                server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(from_email, recipients, msg.as_string())
        log.info("Report email sent to %d recipient(s).", len(recipients))
        return {"sent": True, "recipients": recipients}
    except Exception as exc:  # noqa: BLE001 - email failure must not crash the report run
        log.error("Failed to send report email: %s", exc)
        return {"sent": False, "reason": str(exc), "recipients": recipients}
