from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from applier.config import SourceConfig
from applier.db import Application, Job, ensure_application, utc_now

US_STATE_ABBREVIATIONS = {
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "DC",
}

US_NAMES = (
    "united states",
    "united states of america",
    "u.s.a.",
    "u.s.a",
    "usa",
    "u.s.",
    "remote - us",
    "remote, us",
    "remote (us)",
    "remote us",
    "us remote",
)


@dataclass(slots=True)
class SyncStats:
    downloaded: int = 0
    eligible: int = 0
    inserted: int = 0
    updated: int = 0
    closed: int = 0


def is_us_location(location: str) -> bool:
    normalized = " ".join(location.lower().split())
    if any(name in normalized for name in US_NAMES):
        return True
    if normalized in {"new york city", "washington dc", "washington, d.c."}:
        return True
    return any(
        re.search(rf"(?:,\s*|\b){state.lower()}(?:\s|,|$)", normalized) is not None
        for state in US_STATE_ABBREVIATIONS
    )


def has_us_location(locations: list[str]) -> bool:
    return any(is_us_location(location) for location in locations)


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    clean_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    ]
    return urlunparse(parsed._replace(query=urlencode(clean_query), fragment=""))


def detect_ats(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "greenhouse.io" in host:
        return "greenhouse"
    if host.endswith("lever.co") or ".lever.co" in host:
        return "lever"
    if "ashbyhq.com" in host:
        return "ashby"
    if "myworkdayjobs.com" in host or "workday.com" in host:
        return "workday"
    if "icims.com" in host:
        return "icims"
    if "smartrecruiters.com" in host:
        return "smartrecruiters"
    return "other"


def listing_is_in_scope(item: dict[str, Any], config: SourceConfig) -> bool:
    if item.get("category") not in config.categories:
        return False
    terms = item.get("terms") or []
    if config.term not in terms:
        return False
    if not item.get("active", False) or not item.get("is_visible", True):
        return False
    locations = [str(value) for value in item.get("locations") or []]
    return not config.us_only or has_us_location(locations)


def fetch_listings(
    config: SourceConfig,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    owns_client = client is None
    active_client = client or httpx.Client(follow_redirects=True, timeout=60)
    try:
        response = active_client.get(config.url)
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_client:
            active_client.close()
    if not isinstance(payload, list):
        raise ValueError("Simplify listings feed did not return a JSON array")
    return [item for item in payload if isinstance(item, dict)]


def _set_job_fields(job: Job, item: dict[str, Any], config: SourceConfig) -> None:
    new_url = normalize_url(str(item["url"]))
    if job.url and job.url != new_url:
        job.jd_text = None
        job.jd_fetched_at = None
    job.company = str(item.get("company_name") or "Unknown company")
    job.title = str(item.get("title") or "Unknown role")
    job.url = new_url
    job.ats_type = detect_ats(new_url)
    job.locations_json = json.dumps(item.get("locations") or [], ensure_ascii=False)
    job.source = str(item.get("source") or "Simplify")
    job.term = config.term
    job.date_posted = _as_int(item.get("date_posted"))
    job.date_updated = _as_int(item.get("date_updated"))
    job.active = True
    job.updated_at = utc_now()


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def sync_listings(
    engine: Engine,
    config: SourceConfig,
    *,
    listings: list[dict[str, Any]] | None = None,
) -> SyncStats:
    feed = listings if listings is not None else fetch_listings(config)
    stats = SyncStats(downloaded=len(feed))
    feed_by_id = {str(item.get("id")): item for item in feed if item.get("id")}

    with Session(engine) as session:
        known_jobs = {
            job.id: job for job in session.exec(select(Job).where(Job.term == config.term)).all()
        }
        for listing_id, item in feed_by_id.items():
            if listing_is_in_scope(item, config):
                stats.eligible += 1
                job = known_jobs.get(listing_id)
                if job is None:
                    job = Job(
                        id=listing_id,
                        company="",
                        title="",
                        url="",
                        term=config.term,
                    )
                    session.add(job)
                    known_jobs[listing_id] = job
                    stats.inserted += 1
                else:
                    stats.updated += 1
                _set_job_fields(job, item, config)
                application = ensure_application(session, listing_id)
                if (
                    application.status == "skipped"
                    and application.error == "Listing closed upstream"
                ):
                    application.status = "queued"
                    application.error = None
                    application.updated_at = utc_now()
                continue

            job = known_jobs.get(listing_id)
            if (
                job
                and job.active
                and (item.get("active") is False or item.get("is_visible") is False)
            ):
                job.active = False
                job.updated_at = utc_now()
                existing_application = session.exec(
                    select(Application).where(Application.job_id == listing_id)
                ).first()
                if existing_application and existing_application.status == "queued":
                    existing_application.status = "skipped"
                    existing_application.error = "Listing closed upstream"
                    existing_application.updated_at = utc_now()
                stats.closed += 1

        missing_ids = set(known_jobs) - set(feed_by_id)
        for listing_id in missing_ids:
            job = known_jobs[listing_id]
            if not job.active:
                continue
            job.active = False
            job.updated_at = utc_now()
            existing_application = session.exec(
                select(Application).where(Application.job_id == listing_id)
            ).first()
            if existing_application and existing_application.status == "queued":
                existing_application.status = "skipped"
                existing_application.error = "Listing removed upstream"
                existing_application.updated_at = utc_now()
            stats.closed += 1

        session.commit()
    return stats
