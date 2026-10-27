import httpx

from applier.db import Job
from applier.jd import (
    ashby_api_url,
    fetch_job_description,
    greenhouse_api_url,
    lever_api_url,
    parse_greenhouse_url,
)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_parses_ats_api_urls():
    greenhouse = "https://job-boards.greenhouse.io/acme/jobs/123456"
    assert parse_greenhouse_url(greenhouse) == ("acme", "123456")
    assert (
        greenhouse_api_url(greenhouse)
        == "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123456?content=true"
    )
    assert (
        greenhouse_api_url("https://job-boards.eu.greenhouse.io/acme/jobs/123456")
        == "https://boards.eu.greenhouse.io/v1/boards/acme/jobs/123456?content=true"
    )
    assert (
        lever_api_url("https://jobs.lever.co/acme/abc-123")
        == "https://api.lever.co/v0/postings/acme/abc-123"
    )
    assert (
        lever_api_url("https://jobs.eu.lever.co/acme/abc-123")
        == "https://api.eu.lever.co/v0/postings/acme/abc-123"
    )
    assert (
        ashby_api_url("https://jobs.ashbyhq.com/acme/posting-123")
        == "https://api.ashbyhq.com/posting-api/job-board/acme"
    )


def test_fetches_greenhouse_description():
    job = Job(
        id="1",
        company="Acme",
        title="Intern",
        url="https://job-boards.greenhouse.io/acme/jobs/123",
        ats_type="greenhouse",
    )

    def handler(request):
        assert request.url.host == "boards-api.greenhouse.io"
        return httpx.Response(
            200,
            json={"content": "<p>Build APIs with <strong>Python</strong>.</p>"},
        )

    with _client(handler) as client:
        result = fetch_job_description(job, client)
    assert result.source == "greenhouse_api"
    assert "Build APIs with" in result.text
    assert "Python" in result.text


def test_fetches_lever_and_ashby_descriptions():
    lever_job = Job(
        id="2",
        company="Acme",
        title="Intern",
        url="https://jobs.lever.co/acme/lever-1",
        ats_type="lever",
    )
    ashby_job = Job(
        id="3",
        company="Acme",
        title="Intern",
        url="https://jobs.ashbyhq.com/acme/ashby-1",
        ats_type="ashby",
    )

    def handler(request):
        if request.url.host == "api.lever.co":
            return httpx.Response(
                200,
                json={
                    "descriptionPlain": "Build backend services.",
                    "lists": [{"text": "Requirements", "content": "Python and SQL"}],
                },
            )
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": "ashby-1",
                        "jobUrl": "https://jobs.ashbyhq.com/acme/ashby-1",
                        "descriptionHtml": "<p>Build cloud infrastructure.</p>",
                    }
                ]
            },
        )

    with _client(handler) as client:
        lever = fetch_job_description(lever_job, client)
        ashby = fetch_job_description(ashby_job, client)
    assert lever.source == "lever_api"
    assert "Python and SQL" in lever.text
    assert ashby.source == "ashby_api"
    assert "cloud infrastructure" in ashby.text


def test_falls_back_to_readable_html():
    job = Job(
        id="4",
        company="Acme",
        title="Intern",
        url="https://careers.example.test/job/4",
        ats_type="other",
    )

    def handler(_request):
        return httpx.Response(
            200,
            text="<html><style>bad</style><main><h1>Intern</h1><p>Write Java.</p></main></html>",
        )

    with _client(handler) as client:
        result = fetch_job_description(job, client)
    assert result.source == "html"
    assert "Write Java." in result.text
    assert "bad" not in result.text
