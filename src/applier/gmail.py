from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from applier.db import Application, Job, set_application_status

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SCOPES = [GMAIL_READONLY_SCOPE]

CONFIRMATION_PHRASES = (
    "thank you for applying",
    "thanks for applying",
    "application received",
    "application confirmation",
    "we received your application",
    "your application has been received",
    "application submitted",
)

REJECTION_PHRASES = (
    "not moving forward",
    "pursue other candidates",
    "will not be moving forward",
    "unfortunately",
)


@dataclass(slots=True)
class GmailSyncStats:
    scanned: int = 0
    matched: int = 0


@dataclass(slots=True)
class GmailMessage:
    message_id: str
    subject: str
    sender: str
    sent_at: datetime | None
    body: str


def gmail_service(credentials_path: Path, token_path: Path) -> Resource:
    credentials: Credentials | None = None
    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials or not credentials.valid:
        if not credentials_path.exists():
            raise FileNotFoundError(
                f"Gmail OAuth credentials not found: {credentials_path}. "
                "Download a desktop OAuth client as credentials.json."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
        credentials = flow.run_local_server(port=0)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def _decode_part(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (ValueError, UnicodeError):
        return ""


def extract_message_body(payload: dict[str, Any]) -> str:
    mime_type = str(payload.get("mimeType") or "")
    body_data = (payload.get("body") or {}).get("data")
    if body_data and mime_type in {"text/plain", "text/html"}:
        return _decode_part(str(body_data))
    chunks = [
        extract_message_body(part)
        for part in payload.get("parts") or []
        if isinstance(part, dict)
    ]
    return "\n".join(chunk for chunk in chunks if chunk)


def parse_message(message: dict[str, Any]) -> GmailMessage:
    payload = message.get("payload") or {}
    headers = {
        str(header.get("name", "")).lower(): str(header.get("value", ""))
        for header in payload.get("headers") or []
        if isinstance(header, dict)
    }
    sent_at: datetime | None = None
    if internal_date := message.get("internalDate"):
        try:
            sent_at = datetime.fromtimestamp(int(internal_date) / 1000, tz=UTC)
        except (TypeError, ValueError):
            sent_at = None
    if sent_at is None and headers.get("date"):
        try:
            sent_at = parsedate_to_datetime(headers["date"]).astimezone(UTC)
        except (TypeError, ValueError):
            sent_at = None
    return GmailMessage(
        message_id=str(message.get("id") or ""),
        subject=headers.get("subject", ""),
        sender=headers.get("from", ""),
        sent_at=sent_at,
        body=extract_message_body(payload),
    )


def _normalized_words(value: str) -> list[str]:
    stop = {"inc", "llc", "corp", "corporation", "company", "co", "ltd", "the"}
    return [
        word
        for word in re.findall(r"[a-z0-9]+", value.lower())
        if len(word) > 1 and word not in stop
    ]


def confirmation_score(message: GmailMessage, job: Job, application: Application) -> int:
    subject = message.subject.lower()
    sender = message.sender.lower()
    body = message.body.lower()
    combined = f"{subject}\n{sender}\n{body}"
    if any(phrase in combined for phrase in REJECTION_PHRASES):
        return -100
    if not any(phrase in combined for phrase in CONFIRMATION_PHRASES):
        return 0
    if (
        message.sent_at
        and application.submitted_at
        and message.sent_at < application.submitted_at - timedelta(hours=1)
    ):
        return 0

    score = 4
    company_words = _normalized_words(job.company)
    title_words = _normalized_words(job.title)
    company_hits = sum(word in combined for word in company_words)
    title_hits = sum(word in f"{subject}\n{body}" for word in title_words if len(word) > 3)
    score += min(company_hits * 3, 9)
    score += min(title_hits, 4)
    if any(word in sender for word in company_words):
        score += 3
    if "application" in subject:
        score += 2
    return score


def match_confirmation(
    message: GmailMessage,
    applications: list[tuple[Application, Job]],
) -> tuple[Application, Job] | None:
    scored = [
        (confirmation_score(message, job, application), application, job)
        for application, job in applications
    ]
    if not scored:
        return None
    score, application, job = max(scored, key=lambda item: item[0])
    return (application, job) if score >= 7 else None


def _message_ids(service: Resource, query: str) -> list[str]:
    ids: list[str] = []
    token: str | None = None
    while True:
        response = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=500, pageToken=token)
            .execute()
        )
        ids.extend(str(item["id"]) for item in response.get("messages", []))
        token = response.get("nextPageToken")
        if not token:
            return ids


def sync_gmail_confirmations(
    engine: Engine,
    *,
    credentials_path: Path,
    token_path: Path,
    service: Resource | None = None,
) -> GmailSyncStats:
    active_service = service or gmail_service(credentials_path, token_path)
    query = (
        'newer_than:14d (subject:application OR "thank you for applying" '
        'OR "application received")'
    )
    ids = _message_ids(active_service, query)
    stats = GmailSyncStats(scanned=len(ids))
    with Session(engine) as session:
        rows = session.exec(
            select(Application, Job)
            .join(Job, Application.job_id == Job.id)
            .where(Application.status == "applied")
        ).all()
        candidates = [(application, job) for application, job in rows]
        matched_applications: set[int] = set()
        for message_id in ids:
            raw = (
                active_service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
            message = parse_message(raw)
            match = match_confirmation(
                message,
                [
                    pair
                    for pair in candidates
                    if pair[0].id is not None and pair[0].id not in matched_applications
                ],
            )
            if match is None:
                continue
            application, _ = match
            set_application_status(application, "confirmed")
            if application.id is not None:
                matched_applications.add(application.id)
            stats.matched += 1
        session.commit()
    return stats
