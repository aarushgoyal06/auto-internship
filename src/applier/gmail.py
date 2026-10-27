from __future__ import annotations

import base64
import html
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from applier.db import Application, GmailEvidence, Job, set_application_status

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
    "finish your application",
    "complete your application",
    "application incomplete",
    "saved as draft",
)

ATS_SENDERS = {
    "greenhouse": ("greenhouse.io", "greenhouse-mail.io"),
    "lever": ("hire.lever.co", "lever.co"),
    "ashby": ("ashbyhq.com",),
    "workday": ("myworkday.com",),
    "icims": ("icims.com",),
    "smartrecruiters": ("smartrecruiters.com",),
}
DETECTOR_VERSION = "2"


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
    thread_id: str | None = None


@dataclass(slots=True)
class ConfirmationMatch:
    application: Application
    job: Job
    score: int
    reason: str


def _write_token_securely(token_path: Path, payload: str) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{token_path.name}.",
        dir=token_path.parent,
        text=True,
    )
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temporary_path, token_path)
        token_path.chmod(0o600)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def gmail_service(
    credentials_path: Path,
    token_path: Path,
    *,
    expected_email: str | None = None,
) -> Resource:
    credentials: Credentials | None = None
    if token_path.exists():
        token_path.chmod(0o600)
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
        credentials = flow.run_local_server(
            host="127.0.0.1",
            port=0,
            access_type="offline",
            timeout_seconds=180,
        )
    _write_token_securely(token_path, credentials.to_json())
    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    if expected_email:
        profile = service.users().getProfile(userId="me").execute()
        actual_email = str(profile.get("emailAddress") or "")
        if actual_email.casefold() != expected_email.casefold():
            raise ValueError(
                f"Gmail account mismatch: authorized {actual_email or 'unknown'}, "
                f"expected {expected_email}"
            )
    return service


def _decode_part(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded)
        return decoded[:1_048_576].decode("utf-8", errors="replace")
    except (ValueError, UnicodeError):
        return ""


def extract_message_body(payload: dict[str, Any]) -> str:
    mime_type = str(payload.get("mimeType") or "")
    headers = {
        str(header.get("name") or "").casefold(): str(header.get("value") or "")
        for header in payload.get("headers") or []
        if isinstance(header, dict)
    }
    if payload.get("filename") or "attachment" in headers.get("content-disposition", "").casefold():
        return ""
    body_data = (payload.get("body") or {}).get("data")
    if body_data and mime_type in {"text/plain", "text/html"}:
        decoded = _decode_part(str(body_data))
        if mime_type == "text/html":
            soup = BeautifulSoup(decoded, "html.parser")
            for element in soup(["script", "style", "noscript", "svg"]):
                element.decompose()
            return soup.get_text("\n", strip=True)
        return decoded
    chunks = [
        extract_message_body(part) for part in payload.get("parts") or [] if isinstance(part, dict)
    ]
    return "\n".join(chunk for chunk in chunks if chunk)


def _decode_header_value(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeError):
        return value


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
        thread_id=str(message.get("threadId")) if message.get("threadId") else None,
        subject=_decode_header_value(headers.get("subject", "")),
        sender=_decode_header_value(headers.get("from", "")),
        sent_at=sent_at,
        body=extract_message_body(payload),
    )


def _normalized_words(value: str) -> list[str]:
    stop = {"inc", "llc", "corp", "corporation", "company", "co", "ltd", "the"}
    normalized = unicodedata.normalize("NFKC", html.unescape(value)).casefold()
    return [
        word for word in re.findall(r"[a-z0-9]+", normalized) if len(word) > 1 and word not in stop
    ]


def _normalized_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", html.unescape(value)).casefold()
    return " ".join(re.findall(r"[a-z0-9]+", normalized))


def _posting_key(url: str) -> str | None:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if not parts:
        return None
    candidate = parts[-1].casefold()
    return candidate if len(candidate) >= 6 else None


def _score_confirmation(
    message: GmailMessage,
    job: Job,
    application: Application,
) -> tuple[int, bool, bool, bool, str]:
    subject = message.subject.casefold()
    sender = message.sender.casefold()
    body = message.body.casefold()
    combined = f"{subject}\n{sender}\n{body}"
    if any(phrase in combined for phrase in REJECTION_PHRASES):
        return -100, False, False, False, "negative_pattern"
    if not any(phrase in combined for phrase in CONFIRMATION_PHRASES):
        return 0, False, False, False, "no_confirmation_phrase"
    if message.sent_at and application.submitted_at:
        submitted_at = application.submitted_at
        if submitted_at.tzinfo is None:
            submitted_at = submitted_at.replace(tzinfo=UTC)
        if message.sent_at < submitted_at - timedelta(hours=1):
            return 0, False, False, False, "before_submission"

    score = 10
    reasons = ["confirmation_phrase"]
    normalized_combined = _normalized_text(combined)
    normalized_subject_body = _normalized_text(f"{subject}\n{body}")
    company_words = _normalized_words(job.company)
    company_phrase = " ".join(company_words)
    company_match = bool(company_phrase and company_phrase in normalized_combined)
    if company_match:
        score += 25
        reasons.append("company")

    role_phrase = _normalized_text(job.title)
    role_match = bool(role_phrase and role_phrase in normalized_subject_body)
    if role_match:
        score += 30
        reasons.append("role_exact")
    else:
        role_tokens = {word for word in role_phrase.split() if len(word) > 2}
        message_tokens = set(normalized_subject_body.split())
        overlap = len(role_tokens & message_tokens) / len(role_tokens) if role_tokens else 0
        if overlap >= 0.75:
            role_match = True
            score += 20
            reasons.append("role_tokens")

    posting_key = _posting_key(job.url)
    posting_match = bool(posting_key and posting_key in combined.casefold())
    if posting_match:
        score += 70
        reasons.append("posting_key")

    if any(domain in sender for domain in ATS_SENDERS.get(job.ats_type, ())):
        score += 10
        reasons.append("ats_sender")

    if message.sent_at and application.submitted_at:
        submitted_at = application.submitted_at
        if submitted_at.tzinfo is None:
            submitted_at = submitted_at.replace(tzinfo=UTC)
        delay = message.sent_at - submitted_at
        if delay <= timedelta(hours=6):
            score += 20
            reasons.append("within_6h")
        elif delay <= timedelta(hours=48):
            score += 12
            reasons.append("within_48h")
        elif delay <= timedelta(days=14):
            score += 5
            reasons.append("within_14d")
    if "application" in subject:
        score += 5
        reasons.append("application_subject")
    return score, company_match, role_match, posting_match, ",".join(reasons)


def confirmation_score(message: GmailMessage, job: Job, application: Application) -> int:
    return _score_confirmation(message, job, application)[0]


def match_confirmation_result(
    message: GmailMessage,
    applications: list[tuple[Application, Job]],
) -> ConfirmationMatch | None:
    scored = [
        (*_score_confirmation(message, job, application), application, job)
        for application, job in applications
    ]
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    score, company_match, role_match, posting_match, reason, application, job = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else -100
    has_identity_evidence = posting_match or (company_match and role_match)
    if score < 85 or score - runner_up < 20 or not has_identity_evidence:
        return None
    return ConfirmationMatch(application, job, score, reason)


def match_confirmation(
    message: GmailMessage,
    applications: list[tuple[Application, Job]],
) -> tuple[Application, Job] | None:
    result = match_confirmation_result(message, applications)
    return (result.application, result.job) if result else None


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
            return list(dict.fromkeys(ids))


def sync_gmail_confirmations(
    engine: Engine,
    *,
    credentials_path: Path,
    token_path: Path,
    service: Resource | None = None,
    expected_email: str | None = None,
) -> GmailSyncStats:
    with Session(engine) as session:
        rows = session.exec(
            select(Application, Job)
            .join(Job)
            .where(
                col(Application.status) == "applied",
                col(Application.confirmed_at).is_(None),
            )
        ).all()
        candidates = [(application, job) for application, job in rows]
        if not candidates:
            return GmailSyncStats()

        submitted_times = [
            application.submitted_at
            for application, _ in candidates
            if application.submitted_at is not None
        ]
        cutoff = (
            min(submitted_times) - timedelta(days=2)
            if submitted_times
            else (datetime.now(UTC) - timedelta(days=14))
        )
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
        query = (
            f"after:{int(cutoff.timestamp())} -in:sent -in:drafts "
            '{"thank you for applying" "thanks for applying" '
            '"application received" "application submitted" '
            "from:greenhouse.io from:hire.lever.co from:ashbyhq.com}"
        )
        active_service = service or gmail_service(
            credentials_path,
            token_path,
            expected_email=expected_email,
        )
        ids = _message_ids(active_service, query)
        stats = GmailSyncStats(scanned=len(ids))
        matched_applications: set[int] = set()
        for message_id in ids:
            if session.get(GmailEvidence, message_id):
                continue
            raw = (
                active_service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="full",
                    fields="id,threadId,internalDate,labelIds,snippet,payload",
                )
                .execute()
            )
            message = parse_message(raw)
            available_candidates = [
                pair
                for pair in candidates
                if pair[0].id is not None and pair[0].id not in matched_applications
            ]
            match = match_confirmation_result(
                message,
                available_candidates,
            )
            if match is None:
                best_score = max(
                    (
                        confirmation_score(message, job, application)
                        for application, job in available_candidates
                    ),
                    default=0,
                )
                session.add(
                    GmailEvidence(
                        message_id=message.message_id,
                        thread_id=message.thread_id,
                        received_at=message.sent_at,
                        detector_version=DETECTOR_VERSION,
                        outcome="unmatched",
                        score=best_score,
                        reason="no_high_confidence_match",
                    )
                )
                continue
            application = match.application
            set_application_status(
                application,
                "confirmed",
                occurred_at=message.sent_at,
            )
            if application.id is not None:
                matched_applications.add(application.id)
            session.add(
                GmailEvidence(
                    message_id=message.message_id,
                    thread_id=message.thread_id,
                    received_at=message.sent_at,
                    detector_version=DETECTOR_VERSION,
                    outcome="matched",
                    application_id=application.id,
                    score=match.score,
                    reason=match.reason,
                )
            )
            stats.matched += 1
        session.commit()
    return stats
