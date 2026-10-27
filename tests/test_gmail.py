import base64
import stat
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session

from applier.db import Application, GmailEvidence, Job
from applier.gmail import (
    GmailMessage,
    _write_token_securely,
    match_confirmation,
    parse_message,
    sync_gmail_confirmations,
)


def _encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def test_parses_multipart_gmail_message():
    raw = {
        "id": "message-1",
        "internalDate": "1780000000000",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "Subject", "value": "Application received by Acme"},
                {"name": "From", "value": "Acme Recruiting <jobs@acme.com>"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _encoded("Thank you for applying to Acme.")},
                }
            ],
        },
    }
    message = parse_message(raw)
    assert message.message_id == "message-1"
    assert message.subject == "Application received by Acme"
    assert "Thank you for applying" in message.body
    assert message.sent_at is not None


def test_oauth_token_file_is_written_with_private_permissions(tmp_path: Path):
    token = tmp_path / "gmail_token.json"
    _write_token_securely(token, '{"refresh_token":"secret"}')
    assert token.read_text(encoding="utf-8") == '{"refresh_token":"secret"}'
    assert stat.S_IMODE(token.stat().st_mode) == 0o600


def test_matches_confirmation_to_best_application():
    acme_job = Job(
        id="acme",
        company="Acme",
        title="Software Engineer Intern",
        url="https://acme.example/jobs/1",
    )
    other_job = Job(
        id="other",
        company="Other Corp",
        title="Data Intern",
        url="https://other.example/jobs/2",
    )
    submitted = datetime(2026, 10, 27, tzinfo=UTC)
    acme_application = Application(id=1, job_id="acme", status="applied", submitted_at=submitted)
    other_application = Application(id=2, job_id="other", status="applied", submitted_at=submitted)
    message = GmailMessage(
        message_id="1",
        subject="Thank you for applying to Acme",
        sender="recruiting@acme.com",
        sent_at=datetime(2026, 10, 27, 1, tzinfo=UTC),
        body="We received your application for Software Engineer Intern.",
    )
    match = match_confirmation(
        message,
        [(other_application, other_job), (acme_application, acme_job)],
    )
    assert match is not None
    assert match[0].job_id == "acme"


def test_does_not_treat_rejection_as_confirmation():
    job = Job(id="1", company="Acme", title="Intern", url="https://example.test")
    application = Application(id=1, job_id="1", status="applied")
    message = GmailMessage(
        message_id="1",
        subject="Your application",
        sender="jobs@acme.com",
        sent_at=None,
        body="Unfortunately, we will not be moving forward.",
    )
    assert match_confirmation(message, [(application, job)]) is None


class _Execute:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _Messages:
    def __init__(self, message):
        self.message = message

    def list(self, **_kwargs):
        return _Execute({"messages": [{"id": "message-1"}]})

    def get(self, **_kwargs):
        return _Execute(self.message)


class _Users:
    def __init__(self, message):
        self.message = message

    def messages(self):
        return _Messages(self.message)


class FakeGmailService:
    def __init__(self, message):
        self.message = message

    def users(self):
        return _Users(self.message)


def test_sync_updates_matching_application(engine, tmp_path: Path):
    submitted = datetime(2026, 10, 27, tzinfo=UTC)
    with Session(engine) as session:
        session.add(
            Job(
                id="job-1",
                company="Acme",
                title="Software Engineer Intern",
                url="https://acme.example/jobs/1",
            )
        )
        session.add(
            Application(
                id=1,
                job_id="job-1",
                status="applied",
                submitted_at=submitted,
            )
        )
        session.commit()

    raw = {
        "id": "message-1",
        "internalDate": str(int(datetime(2026, 10, 27, 1, tzinfo=UTC).timestamp() * 1000)),
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": "Thank you for applying to Acme"},
                {"name": "From", "value": "recruiting@acme.com"},
            ],
            "body": {
                "data": _encoded("We received your application for Software Engineer Intern.")
            },
        },
    }
    stats = sync_gmail_confirmations(
        engine,
        credentials_path=tmp_path / "unused.json",
        token_path=tmp_path / "unused-token.json",
        service=FakeGmailService(raw),
    )
    assert stats.scanned == 1
    assert stats.matched == 1
    with Session(engine) as session:
        application = session.get(Application, 1)
        assert application is not None
        assert application.status == "confirmed"
        expected = datetime(2026, 10, 27, 1)
        assert application.confirmed_at == expected
        evidence = session.get(GmailEvidence, "message-1")
        assert evidence is not None
        assert evidence.outcome == "matched"
        assert evidence.application_id == 1
        assert evidence.score >= 85
