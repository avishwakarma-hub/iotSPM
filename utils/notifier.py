from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Any, Dict, Iterable


class Notifier:
    def __init__(self, cfg: Dict[str, Any], logger: logging.Logger | None = None):
        self.cfg = cfg.get("smtp", {})
        self.logger = logger or logging.getLogger(__name__)

    def send(self, subject: str, body: str) -> None:
        if not self.cfg.get("enabled", False):
            self.logger.info("Email disabled; notification skipped: %s", subject)
            return

        host = self.cfg.get("host")
        if not host:
            self.logger.warning(
                "Email enabled but smtp.host is not configured; notification skipped: %s",
                subject,
            )
            return

        recipients = self.cfg.get("alert_email_to", [])
        if isinstance(recipients, str):
            recipients = [recipients]
        recipients = [item for item in recipients if item]
        if not recipients:
            self.logger.warning("No email recipients configured; notification skipped")
            return

        msg = EmailMessage()
        msg["Subject"] = f"[iotSPM] {subject}"
        msg["From"] = self.cfg.get("alert_email_from") or self.cfg.get("username") or "iotspm@localhost"
        msg["To"] = ", ".join(recipients)
        msg.set_content(body)

        try:
            with smtplib.SMTP(host, int(self.cfg.get("port", 587)), timeout=30) as smtp:
                if self.cfg.get("use_tls", True):
                    smtp.starttls()
                username = self.cfg.get("username")
                password = self.cfg.get("password")
                if username and password:
                    smtp.login(username, password)
                smtp.send_message(msg)
            self.logger.info("Sent email notification: %s", subject)
        except Exception as exc:
            self.logger.exception("Failed to send email notification '%s': %s", subject, exc)