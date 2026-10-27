from sqlmodel import Session

from applier.config import SourceConfig
from applier.db import Application, Job
from applier.ingest import (
    detect_ats,
    has_us_location,
    is_us_location,
    normalize_url,
    sync_listings,
)


def _listing(**overrides):
    value = {
        "id": "job-1",
        "source": "Simplify",
        "category": "Software",
        "company_name": "Example",
        "title": "Software Engineer Intern",
        "active": True,
        "is_visible": True,
        "terms": ["Summer 2027"],
        "date_posted": 1_782_000_000,
        "date_updated": 1_782_000_100,
        "url": "https://job-boards.greenhouse.io/example/jobs/123?utm_source=github",
        "locations": ["Austin, TX"],
    }
    value.update(overrides)
    return value


def test_us_location_filter_is_strict():
    assert is_us_location("New York, NY")
    assert is_us_location("Remote - US")
    assert is_us_location("Washington, DC, United States")
    assert not is_us_location("Toronto, ON, Canada")
    assert not is_us_location("Remote")
    assert not has_us_location(["Vancouver, BC, Canada", "London, UK"])


def test_url_normalization_and_ats_detection():
    url = normalize_url(
        "https://jobs.lever.co/acme/123?utm_source=github&ref=keep&utm_medium=list#apply"
    )
    assert url == "https://jobs.lever.co/acme/123?ref=keep"
    assert detect_ats(url) == "lever"
    assert detect_ats("https://jobs.ashbyhq.com/acme/123") == "ashby"
    assert detect_ats("https://acme.wd5.myworkdayjobs.com/job") == "workday"
    assert detect_ats("https://careers.example.com/job") == "other"


def test_sync_inserts_closes_and_reopens(engine):
    config = SourceConfig(url="https://example.test/feed.json")
    first = sync_listings(engine, config, listings=[_listing()])
    assert first.downloaded == 1
    assert first.eligible == 1
    assert first.inserted == 1

    with Session(engine) as session:
        job = session.get(Job, "job-1")
        assert job is not None
        assert job.active
        assert job.ats_type == "greenhouse"
        assert job.locations == ["Austin, TX"]
        application = session.get(Application, 1)
        assert application is not None
        assert application.status == "queued"

    closed = sync_listings(engine, config, listings=[_listing(active=False)])
    assert closed.closed == 1
    with Session(engine) as session:
        job = session.get(Job, "job-1")
        application = session.get(Application, 1)
        assert job is not None and not job.active
        assert application is not None and application.status == "skipped"

    reopened = sync_listings(engine, config, listings=[_listing(active=True)])
    assert reopened.eligible == 1
    with Session(engine) as session:
        job = session.get(Job, "job-1")
        application = session.get(Application, 1)
        assert job is not None and job.active
        assert application is not None and application.status == "queued"
