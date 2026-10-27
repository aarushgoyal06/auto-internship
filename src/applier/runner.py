from __future__ import annotations

import hashlib
import random
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Browser, sync_playwright
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from applier.answers import AnswerResolver
from applier.ats import adapter_for
from applier.ats.base import ApplicationContext, ApplyResult
from applier.config import Profile, Settings
from applier.db import (
    Application,
    Artifact,
    Attempt,
    Job,
    set_application_status,
    utc_now,
)
from applier.jd import fetch_job_description, update_job_description
from applier.llm import (
    AnswerContext,
    LLMError,
    LLMProvider,
    build_provider,
    extract_llm_keywords,
)
from applier.tailor import ResumeTailor


@dataclass(slots=True)
class RunSummary:
    counts: Counter[str] = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


class ApplicationRunner:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        profile: Profile,
        *,
        provider: LLMProvider | None = None,
        auto_provider: bool = True,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.profile = profile
        self.settings.ensure_runtime_dirs()
        self.tailor = ResumeTailor(output_dir=settings.paths.resumes)
        self.provider = provider
        self.llm_warning: str | None = None
        if self.provider is None and auto_provider:
            try:
                self.provider = build_provider(settings.llm)
            except LLMError as error:
                self.llm_warning = str(error)

    def _resume_facts(self) -> str:
        data = self.tailor._load_resume_data()
        facts = [self.profile.factual_context()]
        for role in data.get("experience", []):
            facts.append(
                f"{role.get('role')} at {role.get('company')} ({role.get('dates')}): "
                + " ".join(str(item.get("text", "")) for item in role.get("bullets", []))
            )
            for subrole in role.get("subroles", []):
                facts.append(
                    f"{subrole.get('role')} at {role.get('company')} ({subrole.get('dates')}): "
                    + " ".join(str(item.get("text", "")) for item in subrole.get("bullets", []))
                )
        return "\n".join(facts)

    def _answer_context(self, job: Job) -> AnswerContext:
        return AnswerContext(
            company=job.company,
            role=job.title,
            job_description=job.jd_text or "",
            resume_facts=self._resume_facts(),
        )

    @staticmethod
    def _add_artifact(
        session: Session,
        application: Application,
        kind: str,
        path: Path | None,
    ) -> None:
        if path is None or application.id is None or not path.exists():
            return
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        session.add(
            Artifact(
                application_id=application.id,
                kind=kind,
                path=str(path),
                sha256=digest,
            )
        )

    def _prepare_resume(
        self,
        session: Session,
        job: Job,
        application: Application,
    ) -> Path:
        if not job.jd_text:
            description = fetch_job_description(job)
            update_job_description(job, description)
            session.add(job)
            session.commit()

        extra_keywords: list[str] = []
        if self.provider is not None and job.jd_text:
            try:
                extra_keywords = extract_llm_keywords(
                    self.provider,
                    job.jd_text,
                    self.profile,
                )
            except Exception:
                extra_keywords = []
        resume = self.tailor.tailor(
            job,
            self.profile,
            extra_keywords=extra_keywords,
        )
        application.resume_path = str(resume.pdf_path)
        application.updated_at = utc_now()
        session.add(application)
        if application.id is not None:
            self._add_artifact(session, application, "resume_pdf", resume.pdf_path)
            self._add_artifact(session, application, "resume_tex", resume.tex_path)
        session.commit()
        return resume.pdf_path

    def _application_context(
        self,
        job: Job,
        application: Application,
        resume_path: Path,
    ) -> ApplicationContext:
        resolver = AnswerResolver(
            self.profile,
            self._answer_context(job),
            self.provider,
        )
        return ApplicationContext(
            job=job,
            application=application,
            resume_path=resume_path,
            resolver=resolver,
            screenshot_dir=self.settings.paths.screenshots,
            trace_dir=self.settings.paths.traces,
            navigation_timeout_ms=self.settings.runtime.navigation_timeout_ms,
        )

    def _record_result(
        self,
        session: Session,
        application: Application,
        attempt: Attempt,
        result: ApplyResult,
        context: ApplicationContext,
        *,
        dry_run: bool,
    ) -> None:
        attempt.finished_at = utc_now()
        attempt.outcome = "dry_run" if dry_run and result.status == "queued" else result.status
        attempt.error = None if result.status in {"applied", "queued"} else result.message
        attempt.screenshot_path = str(result.screenshot_path) if result.screenshot_path else None
        attempt.trace_path = str(result.trace_path) if result.trace_path else None
        application.answers = context.resolver.audit
        if not (dry_run and result.status == "queued"):
            set_application_status(
                application,
                result.status,
                error=None if result.status in {"applied", "confirmed"} else result.message,
            )
        else:
            application.error = None
            application.updated_at = utc_now()
        session.add(attempt)
        session.add(application)
        self._add_artifact(session, application, "screenshot", result.screenshot_path)
        self._add_artifact(session, application, "playwright_trace", result.trace_path)
        session.commit()

    def _process_application(
        self,
        session: Session,
        browser: Browser,
        application: Application,
        job: Job,
        *,
        dry_run: bool,
    ) -> ApplyResult:
        try:
            resume_path = self._prepare_resume(session, job, application)
        except Exception as error:
            result = ApplyResult("failed", f"Preparation failed: {type(error).__name__}: {error}")
            if application.id is None:
                raise RuntimeError("Application has no database id") from error
            attempt = Attempt(
                application_id=application.id,
                finished_at=utc_now(),
                outcome="failed",
                error=result.message,
            )
            set_application_status(application, "failed", error=result.message)
            session.add(attempt)
            session.add(application)
            session.commit()
            return result

        context = self._application_context(job, application, resume_path)
        adapter = adapter_for(job.ats_type)
        if application.id is None:
            raise RuntimeError("Application has no database id")
        last_result = ApplyResult("failed", "No application attempt was made")
        for attempt_number in range(self.settings.runtime.max_retries + 1):
            attempt = Attempt(application_id=application.id)
            session.add(attempt)
            session.commit()
            last_result = adapter.apply(browser, context, dry_run=dry_run)
            self._record_result(
                session,
                application,
                attempt,
                last_result,
                context,
                dry_run=dry_run,
            )
            if (
                last_result.status != "failed"
                or attempt_number >= self.settings.runtime.max_retries
            ):
                break
        return last_result

    def apply(
        self,
        *,
        limit: int | None = None,
        ats_type: str | None = None,
        dry_run: bool = False,
    ) -> RunSummary:
        summary = RunSummary()
        effective_limit = limit or self.settings.runtime.max_applications
        with Session(self.engine) as session:
            statement = (
                select(Application, Job)
                .join(Job)
                .where(
                    col(Application.status) == "queued",
                    col(Job.active).is_(True),
                )
                .order_by(col(Job.date_posted).desc())
            )
            if ats_type:
                statement = statement.where(col(Job.ats_type) == ats_type)
            if effective_limit is not None:
                statement = statement.limit(effective_limit)
            rows = list(session.exec(statement).all())

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=self.settings.runtime.headless,
                    slow_mo=self.settings.runtime.slow_mo_ms,
                )
                try:
                    for index, (application, job) in enumerate(rows):
                        result = self._process_application(
                            session,
                            browser,
                            application,
                            job,
                            dry_run=dry_run,
                        )
                        summary.counts[result.status] += 1
                        if not dry_run and index < len(rows) - 1:
                            delay = random.uniform(
                                self.settings.runtime.min_delay_seconds,
                                self.settings.runtime.max_delay_seconds,
                            )
                            time.sleep(delay)
                finally:
                    browser.close()
        return summary

    def review(
        self,
        confirm_submission: Callable[[Job, list[str]], bool],
        *,
        limit: int | None = None,
    ) -> RunSummary:
        summary = RunSummary()
        with Session(self.engine) as session:
            statement = (
                select(Application, Job)
                .join(Job)
                .where(
                    col(Application.status) == "needs_review",
                    col(Job.active).is_(True),
                )
                .order_by(col(Job.date_posted).desc())
            )
            if limit is not None:
                statement = statement.limit(limit)
            rows = list(session.exec(statement).all())
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=False)
                try:
                    for application, job in rows:
                        resume_path = (
                            Path(application.resume_path)
                            if application.resume_path
                            else self._prepare_resume(session, job, application)
                        )
                        context = self._application_context(job, application, resume_path)
                        adapter = adapter_for(job.ats_type)
                        browser_context = browser.new_context()
                        page = browser_context.new_page()
                        unresolved: list[str] = []
                        try:
                            page.goto(
                                job.url,
                                wait_until="domcontentloaded",
                                timeout=self.settings.runtime.navigation_timeout_ms,
                            )
                            adapter.dismiss_cookie_banners(page)
                            adapter.prepare_page(page)
                            unresolved = adapter.prefill(page, context, ignore_captcha=True)
                            submitted = confirm_submission(job, unresolved)
                            if submitted:
                                application.answers = context.resolver.audit
                                set_application_status(application, "applied")
                                summary.counts["applied"] += 1
                            else:
                                summary.counts["needs_review"] += 1
                            session.add(application)
                            session.commit()
                        finally:
                            browser_context.close()
                finally:
                    browser.close()
        return summary
