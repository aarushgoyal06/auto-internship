from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

from applier.db import Job


@dataclass(slots=True)
class JobDescription:
    text: str
    source: str


def clean_html(raw_html: str) -> str:
    soup = BeautifulSoup(html.unescape(raw_html), "html.parser")
    for element in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        element.decompose()
    text = soup.get_text("\n")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def parse_greenhouse_url(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    parts = [part for part in parsed.path.split("/") if part]
    board = query.get("for", [None])[0]
    job_id = query.get("gh_jid", query.get("token", [None]))[0]
    if "jobs" in parts:
        index = parts.index("jobs")
        if index > 0:
            board = parts[index - 1]
        if len(parts) > index + 1:
            job_id = parts[index + 1]
    elif parts and board is None:
        board = parts[0]
    if board and job_id:
        return board, re.sub(r"\D.*$", "", str(job_id)) or str(job_id)
    return None


def parse_lever_url(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None


def parse_ashby_url(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None


def greenhouse_api_url(url: str) -> str | None:
    parsed = parse_greenhouse_url(url)
    if not parsed:
        return None
    board, job_id = parsed
    return f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?content=true"


def lever_api_url(url: str) -> str | None:
    parsed = parse_lever_url(url)
    if not parsed:
        return None
    company, posting_id = parsed
    return f"https://api.lever.co/v0/postings/{company}/{posting_id}"


def ashby_api_url(url: str) -> str | None:
    parsed = parse_ashby_url(url)
    if not parsed:
        return None
    company, _ = parsed
    return f"https://api.ashbyhq.com/posting-api/job-board/{company}"


def _greenhouse_description(job: Job, client: httpx.Client) -> JobDescription | None:
    endpoint = greenhouse_api_url(job.url)
    if not endpoint:
        return None
    response = client.get(endpoint)
    if response.status_code != 200:
        return None
    payload = response.json()
    content = payload.get("content") if isinstance(payload, dict) else None
    if content:
        return JobDescription(clean_html(str(content)), "greenhouse_api")
    return None


def _lever_description(job: Job, client: httpx.Client) -> JobDescription | None:
    endpoint = lever_api_url(job.url)
    if not endpoint:
        return None
    response = client.get(endpoint)
    if response.status_code != 200:
        return None
    payload = response.json()
    if not isinstance(payload, dict):
        return None
    chunks = [
        payload.get("descriptionPlain") or payload.get("description"),
        payload.get("additionalPlain") or payload.get("additional"),
    ]
    for item in payload.get("lists") or []:
        if isinstance(item, dict):
            chunks.extend((item.get("text"), item.get("content")))
    text = "\n".join(clean_html(str(chunk)) for chunk in chunks if chunk)
    return JobDescription(text, "lever_api") if text.strip() else None


def _ashby_description(job: Job, client: httpx.Client) -> JobDescription | None:
    endpoint = ashby_api_url(job.url)
    parsed = parse_ashby_url(job.url)
    if not endpoint or not parsed:
        return None
    _, posting_id = parsed
    response = client.get(endpoint)
    if response.status_code != 200:
        return None
    payload = response.json()
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    for item in jobs:
        if not isinstance(item, dict):
            continue
        candidate_urls = [
            str(item.get("jobUrl") or ""),
            str(item.get("applyUrl") or ""),
            str(item.get("id") or ""),
        ]
        if not any(posting_id in value for value in candidate_urls):
            continue
        content = item.get("descriptionPlain") or item.get("descriptionHtml")
        if content:
            return JobDescription(clean_html(str(content)), "ashby_api")
    return None


def _html_description(job: Job, client: httpx.Client) -> JobDescription:
    response = client.get(job.url)
    response.raise_for_status()
    text = clean_html(response.text)
    if not text:
        raise ValueError(f"No readable job description found at {job.url}")
    return JobDescription(text, "html")


def fetch_job_description(
    job: Job,
    client: httpx.Client | None = None,
) -> JobDescription:
    owns_client = client is None
    active_client = client or httpx.Client(
        follow_redirects=True,
        timeout=45,
        headers={"User-Agent": "Mozilla/5.0 (compatible; InternshipApplier/0.1)"},
    )
    try:
        fetchers = {
            "greenhouse": _greenhouse_description,
            "lever": _lever_description,
            "ashby": _ashby_description,
        }
        result = None
        if fetcher := fetchers.get(job.ats_type):
            try:
                result = fetcher(job, active_client)
            except (httpx.HTTPError, ValueError):
                result = None
        return result or _html_description(job, active_client)
    finally:
        if owns_client:
            active_client.close()


def update_job_description(job: Job, description: JobDescription) -> None:
    job.jd_text = description.text
    job.jd_fetched_at = datetime.now(UTC)
