import os
import smtplib
from email.message import EmailMessage

import requests
from dotenv import load_dotenv

load_dotenv(override=True)

EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS")
EMAIL_SMTP_SERVER = os.getenv("EMAIL_SMTP_SERVER")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD")
DEFAULT_EMAIL_TO = os.getenv("DEFAULT_EMAIL_TO", EMAIL_ADDRESS or "")


def email_configured() -> bool:
    return bool(EMAIL_ADDRESS and EMAIL_SMTP_SERVER and EMAIL_APP_PASSWORD)


def send_email(subject: str, text_body: str, html_body: str, to: str | None = None) -> None:
    if not email_configured():
        raise RuntimeError("Email is not configured (EMAIL_ADDRESS / EMAIL_SMTP_SERVER / EMAIL_APP_PASSWORD)")
    msg = EmailMessage()
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = to or DEFAULT_EMAIL_TO
    msg["Subject"] = subject
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(EMAIL_SMTP_SERVER, 587, timeout=30) as server:
        server.starttls()
        server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
        server.send_message(msg)


pushover_user = os.getenv("PUSHOVER_USER")
pushover_token = os.getenv("PUSHOVER_TOKEN")
pushover_url = "https://api.pushover.net/1/messages.json"


def push(message: str) -> None:
    if not (pushover_user and pushover_token):
        return
    payload = {"user": pushover_user, "token": pushover_token, "message": message}
    requests.post(pushover_url, data=payload, timeout=15)
