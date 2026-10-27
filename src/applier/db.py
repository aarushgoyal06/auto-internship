from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, create_engine, select


def utc_now() -> datetime:
    return datetime.now(UTC)


class Job(SQLModel, table=True):
    id: str = Field(primary_key=True)
    company: str = Field(index=True)
    title: str = Field(index=True)
    url: str
    ats_type: str = Field(default="other", index=True)
    locations_json: str = "[]"
    source: str = "Simplify"
    term: str = "Summer 2027"
    date_posted: int | None = None
    date_updated: int | None = None
    active: bool = Field(default=True, index=True)
    jd_text: str | None = None
    jd_fetched_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def locations(self) -> list[str]:
        value = json.loads(self.locations_json or "[]")
        return [str(item) for item in value]

    @locations.setter
    def locations(self, value: list[str]) -> None:
        self.locations_json = json.dumps(value, ensure_ascii=False)


class Application(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    job_id: str = Field(foreign_key="job.id", unique=True, index=True)
    status: str = Field(default="queued", index=True)
    resume_path: str | None = None
    answers_json: str = "{}"
    error: str | None = None
    submitted_at: datetime | None = None
    confirmed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def answers(self) -> dict[str, Any]:
        value = json.loads(self.answers_json or "{}")
        return value if isinstance(value, dict) else {}

    @answers.setter
    def answers(self, value: dict[str, Any]) -> None:
        self.answers_json = json.dumps(value, ensure_ascii=False)


class Attempt(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="application.id", index=True)
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    outcome: str = "started"
    error: str | None = None
    screenshot_path: str | None = None
    trace_path: str | None = None


class Artifact(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="application.id", index=True)
    kind: str = Field(index=True)
    path: str
    sha256: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class GmailEvidence(SQLModel, table=True):
    message_id: str = Field(primary_key=True)
    thread_id: str | None = None
    received_at: datetime | None = None
    detector_version: str = "1"
    outcome: str = Field(index=True)
    application_id: int | None = Field(
        default=None,
        foreign_key="application.id",
        index=True,
    )
    score: int = 0
    reason: str
    processed_at: datetime = Field(default_factory=utc_now)


APPLICATION_STATUSES = {
    "queued",
    "applied",
    "failed",
    "needs_review",
    "skipped",
    "confirmed",
}


def create_db_engine(path: Path) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(
        f"sqlite:///{path.resolve()}",
        connect_args={"check_same_thread": False},
    )


def init_db(engine: Engine) -> None:
    SQLModel.metadata.create_all(engine)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def ensure_application(session: Session, job_id: str) -> Application:
    existing = session.exec(select(Application).where(Application.job_id == job_id)).first()
    if existing:
        return existing
    application = Application(job_id=job_id)
    session.add(application)
    session.flush()
    return application


def set_application_status(
    application: Application,
    status: str,
    *,
    error: str | None = None,
    occurred_at: datetime | None = None,
) -> None:
    if status not in APPLICATION_STATUSES:
        raise ValueError(f"Unknown application status: {status}")
    application.status = status
    application.error = error
    application.updated_at = utc_now()
    if status == "applied":
        application.submitted_at = occurred_at or utc_now()
    elif status == "confirmed":
        application.confirmed_at = occurred_at or utc_now()
