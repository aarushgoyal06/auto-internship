import base64
from datetime import UTC, datetime

from applier.db import Application, Job
from applier.gmail import GmailMessage, match_confirmation, parse_message


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
