from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from applier.answers import AnswerResolver
from applier.ats.ashby import AshbyAdapter
from applier.ats.base import ApplicationContext
from applier.ats.greenhouse import GreenhouseAdapter
from applier.ats.lever import LeverAdapter
from applier.ats.manual import ManualReviewAdapter
from applier.captcha import detect_captcha
from applier.db import Application, Job
from applier.llm import AnswerContext


class FakeProvider:
    def complete(self, _prompt: str) -> str:
        return "I want to apply my Python, AWS, and automation experience."


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        yield instance
        instance.close()


def _context(tmp_path: Path, profile, ats: str, fixture_name: str) -> ApplicationContext:
    fixture = Path("tests/fixtures") / fixture_name
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n% test resume")
    job = Job(
        id=f"{ats}-job",
        company="Acme",
        title="Software Engineer Intern",
        url=fixture.resolve().as_uri(),
        ats_type=ats,
        jd_text="Build Python services on AWS.",
    )
    application = Application(id=1, job_id=job.id)
    resolver = AnswerResolver(
        profile,
        AnswerContext(
            company=job.company,
            role=job.title,
            job_description=job.jd_text,
            resume_facts=profile.factual_context(),
        ),
        FakeProvider(),
    )
    return ApplicationContext(
        job=job,
        application=application,
        resume_path=resume,
        resolver=resolver,
        screenshot_dir=tmp_path / "screenshots",
        trace_dir=tmp_path / "traces",
        navigation_timeout_ms=5_000,
    )


@pytest.mark.parametrize(
    ("adapter", "ats", "fixture_name"),
    [
        (GreenhouseAdapter(), "greenhouse", "greenhouse_form.html"),
        (LeverAdapter(), "lever", "lever_form.html"),
        (AshbyAdapter(), "ashby", "ashby_form.html"),
    ],
)
def test_supported_adapter_submits_fixture(
    browser,
    profile,
    tmp_path,
    adapter,
    ats,
    fixture_name,
):
    context = _context(tmp_path, profile, ats, fixture_name)
    result = adapter.apply(browser, context)
    assert result.status == "applied", result.message
    assert result.screenshot_path is not None and result.screenshot_path.exists()
    assert result.trace_path is not None and result.trace_path.exists()
    assert context.resolver.audit["Email"]["source"] == "profile"
    if ats == "ashby":
        assert context.resolver.audit["Will you require sponsorship?"]["answer"] == "No"


def test_dry_run_never_clicks_submit(browser, profile, tmp_path):
    context = _context(tmp_path, profile, "greenhouse", "greenhouse_form.html")
    result = GreenhouseAdapter().apply(browser, context, dry_run=True)
    assert result.status == "queued"
    assert "not clicked" in result.message


def test_blank_page_is_not_reported_as_success(browser, profile, tmp_path):
    blank = tmp_path / "blank.html"
    blank.write_text("<!doctype html><html><body></body></html>", encoding="utf-8")
    context = _context(tmp_path, profile, "ashby", "ashby_form.html")
    context.job.url = blank.resolve().as_uri()
    result = AshbyAdapter().apply(browser, context, dry_run=True)
    assert result.status == "needs_review"
    assert result.message == "Visible application form was not found"
    assert result.screenshot_path is not None


def test_visible_captcha_is_detected_but_hidden_token_is_not(browser):
    context = browser.new_context()
    page = context.new_page()
    page.set_content('<textarea name="g-recaptcha-response" style="display:none"></textarea>')
    assert detect_captcha(page) is None
    page.set_content(
        '<iframe src="https://www.google.com/recaptcha/api2/anchor" '
        'style="width:256px;height:60px"></iframe>'
    )
    assert detect_captcha(page) is None
    page.set_content(
        '<iframe src="https://www.google.com/recaptcha/api2/bframe" '
        'style="width:300px;height:300px"></iframe>'
    )
    assert detect_captcha(page) == "reCAPTCHA"
    page.set_content('<div class="h-captcha" style="width:100px;height:50px">Challenge</div>')
    assert detect_captcha(page) == "hCaptcha"
    context.close()


def test_unknown_ats_routes_to_manual_review(browser, profile, tmp_path):
    context = _context(tmp_path, profile, "other", "lever_form.html")
    result = ManualReviewAdapter("Custom").apply(browser, context)
    assert result.status == "needs_review"
    assert "does not have" in result.message
