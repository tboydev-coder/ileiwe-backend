import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from html import escape
from .core.config import get_settings
from .storage import Storage


# SMS delivery is intentionally disabled for now.
# class SMSProvider(Protocol):
#     def send(self, recipient: str, message: str, key: str) -> str: ...
#
# class TermiiSMS:
#     ...


def deliver(notification, attachment=None):
    settings = get_settings()
    if notification.channel == "SMS":
        raise RuntimeError("SMS delivery is temporarily disabled.")
    message = EmailMessage()
    message["From"] = formataddr((settings.smtp_from_name, settings.smtp_from_email))
    message["To"] = notification.recipient
    message["Subject"] = notification.subject
    message["Message-ID"] = f"<{notification.id}@ile-iwe.local>"
    message.set_content(notification.body)
    if getattr(notification, "school_logo", None):
        message.add_alternative(
            '<html><body><img src="cid:school-logo" alt="School logo" width="100"><p>'
            + escape(notification.body).replace("\n", "<br>")
            + "</p></body></html>",
            subtype="html",
        )
        message.get_payload()[-1].add_related(
            notification.school_logo, maintype="image", subtype="png", cid="<school-logo>"
        )
    if attachment:
        content, filename, mime = attachment
        main, sub = mime.split("/", 1)
        message.add_attachment(content, maintype=main, subtype=sub, filename=filename)
    if not settings.email_enabled:
        if settings.app_env == "production":
            raise RuntimeError("Email delivery is not configured.")
        # Private development outbox; never emit message bodies or reset tokens to logs.
        Storage().put(f"{notification.school_id}/outbox/{notification.id}.eml", message.as_bytes(), "message/rfc822")
        return "SIMULATED", "development:" + notification.id
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as server:
        if settings.smtp_use_tls:
            server.starttls()
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(message)
    return "SENT", str(message["Message-ID"])
