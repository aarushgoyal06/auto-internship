from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import Browser, Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from applier.answers import AnswerResolver, FormQuestion
from applier.captcha import detect_captcha
from applier.db import Application, Job


@dataclass(slots=True)
class ApplicationContext:
    job: Job
    application: Application
    resume_path: Path
    resolver: AnswerResolver
    screenshot_dir: Path
    trace_dir: Path
    navigation_timeout_ms: int = 45_000


@dataclass(slots=True)
class ApplyResult:
    status: str
    message: str
    screenshot_path: Path | None = None
    trace_path: Path | None = None


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()[:70]


def _field_label(locator: Locator) -> str:
    try:
        value = locator.evaluate(
            """el => {
                const labels = el.labels ? Array.from(el.labels)
                    .map(label => label.innerText.trim()).filter(Boolean) : [];
                const fieldset = el.closest('fieldset');
                const legend = fieldset?.querySelector('legend')?.innerText?.trim();
                const container = el.closest(
                    '.application-question, .custom-question, .field, ' +
                    '[data-testid*="question"], .ashby-application-form-question-container'
                );
                const containerLabel = container?.querySelector(
                    'label, .application-label, [data-testid*="label"]'
                )?.innerText?.trim();
                return labels.join(' ') || legend || containerLabel ||
                    el.getAttribute('aria-label') ||
                    el.getAttribute('data-label') || el.getAttribute('placeholder') ||
                    el.getAttribute('name') || el.getAttribute('id') || '';
            }"""
        )
    except Exception:
        value = ""
    return re.sub(r"\s+", " ", str(value)).strip().rstrip("*").strip()


def _radio_group_label(locator: Locator) -> str:
    try:
        value = locator.evaluate(
            """el => {
                const fieldset = el.closest('fieldset');
                const group = el.closest('[role="radiogroup"]');
                return fieldset?.querySelector('legend')?.innerText?.trim() ||
                    group?.getAttribute('aria-label') ||
                    group?.getAttribute('data-label') || '';
            }"""
        )
    except Exception:
        value = ""
    return re.sub(r"\s+", " ", str(value)).strip().rstrip("*").strip()


def _is_required(locator: Locator, label: str = "") -> bool:
    try:
        return (
            locator.get_attribute("required") is not None
            or locator.get_attribute("aria-required") == "true"
            or label.rstrip().endswith("*")
            or bool(
                locator.evaluate(
                    """el => Boolean(el.closest(
                        '[data-required="true"], .required, .field--required, ' +
                        '.ashby-application-form-question-container'
                    )?.querySelector('[aria-required="true"], [required]'))"""
                )
            )
        )
    except Exception:
        return False


def _visible(locator: Locator) -> bool:
    try:
        return locator.is_visible() and locator.is_enabled()
    except Exception:
        return False


class BaseATSAdapter:
    name = "base"
    form_selectors: tuple[str, ...] = ("form",)
    submit_selectors: tuple[str, ...] = (
        'button[type="submit"]',
        'input[type="submit"]',
    )
    confirmation_patterns: tuple[str, ...] = (
        "thank you for applying",
        "application has been submitted",
        "application was submitted",
        "successfully submitted",
        "thanks for applying",
    )
    closed_patterns: tuple[str, ...] = (
        "this job is no longer available",
        "this position has been filled",
        "job is no longer available",
        "applications are closed",
    )

    def matches(self, job: Job) -> bool:
        return job.ats_type == self.name

    def prepare_page(self, page: Page) -> None:
        """Open or reveal the application form when an ATS starts on a job detail page."""

    def dismiss_cookie_banners(self, page: Page) -> None:
        for pattern in ("Accept all", "Accept cookies", "Allow all"):
            try:
                button = page.get_by_role("button", name=pattern, exact=False)
                if button.count() and button.first.is_visible():
                    button.first.click(timeout=1_000)
                    return
            except Exception:
                continue

    def _page_is_closed(self, page: Page) -> bool:
        try:
            text = page.locator("body").inner_text(timeout=3_000).lower()
        except Exception:
            return False
        return any(pattern in text for pattern in self.closed_patterns)

    def _application_form_present(self, page: Page) -> bool:
        combined = ", ".join(self.form_selectors)
        with suppress(PlaywrightTimeoutError):
            page.wait_for_selector(combined, state="visible", timeout=3_000)
        for selector in self.form_selectors:
            candidates = page.locator(selector)
            for index in range(candidates.count()):
                if _visible(candidates.nth(index)):
                    return True
        return False

    def _fill_files(
        self,
        page: Page,
        context: ApplicationContext,
        unresolved: list[str],
    ) -> None:
        files = page.locator('input[type="file"]')
        for index in range(files.count()):
            field = files.nth(index)
            label = _field_label(field)
            key = f"{label} {field.get_attribute('name') or ''}".lower()
            if "resume" in key or "cv" in key:
                field.set_input_files(str(context.resume_path))
            elif "cover" in key:
                if _is_required(field, label):
                    unresolved.append(label or "Required cover letter")
            elif _is_required(field, label):
                unresolved.append(label or "Unknown required file")

    def _fill_text_fields(
        self,
        page: Page,
        context: ApplicationContext,
        unresolved: list[str],
    ) -> None:
        fields = page.locator(
            'input:not([type="hidden"]):not([type="file"]):not([type="radio"])'
            ':not([type="checkbox"]):not([type="submit"]):not([type="button"]), textarea'
        )
        for index in range(fields.count()):
            field = fields.nth(index)
            if not _visible(field):
                continue
            try:
                if field.input_value().strip():
                    continue
            except Exception:
                pass
            label = _field_label(field)
            field_type = (
                "textarea"
                if field.evaluate("el => el.tagName") == "TEXTAREA"
                else (field.get_attribute("type") or "text")
            )
            max_length_raw = field.get_attribute("maxlength")
            max_length = (
                int(max_length_raw) if max_length_raw and max_length_raw.isdigit() else 1000
            )
            required = _is_required(field, label)
            answer = context.resolver.resolve(
                FormQuestion(
                    label=label,
                    field_type=field_type,
                    required=required,
                    max_length=max_length,
                )
            )
            if answer is not None:
                field.fill(answer.value)
            elif required:
                unresolved.append(label or field.get_attribute("name") or "Required text field")

    def _fill_selects(
        self,
        page: Page,
        context: ApplicationContext,
        unresolved: list[str],
    ) -> None:
        selects = page.locator("select")
        for index in range(selects.count()):
            field = selects.nth(index)
            if not _visible(field):
                continue
            label = _field_label(field)
            options = [value.strip() for value in field.locator("option").all_inner_texts()]
            required = _is_required(field, label)
            answer = context.resolver.resolve(
                FormQuestion(
                    label=label,
                    field_type="select",
                    options=[option for option in options if option],
                    required=required,
                )
            )
            if answer is not None:
                try:
                    field.select_option(label=answer.value)
                except Exception:
                    matched = next(
                        (
                            option
                            for option in options
                            if answer.value.lower() in option.lower()
                            or option.lower() in answer.value.lower()
                        ),
                        None,
                    )
                    if matched:
                        field.select_option(label=matched)
                    elif required:
                        unresolved.append(label)
            elif required:
                current = field.input_value()
                if not current:
                    unresolved.append(label or field.get_attribute("name") or "Required select")

    def _fill_radios(
        self,
        page: Page,
        context: ApplicationContext,
        unresolved: list[str],
    ) -> None:
        radios = page.locator('input[type="radio"]')
        groups: dict[str, list[Locator]] = {}
        for index in range(radios.count()):
            radio = radios.nth(index)
            name = radio.get_attribute("name") or f"radio-{index}"
            groups.setdefault(name, []).append(radio)
        for name, fields in groups.items():
            visible_fields = [field for field in fields if _visible(field)]
            if not visible_fields or any(field.is_checked() for field in visible_fields):
                continue
            label = _radio_group_label(visible_fields[0]) or name
            options: list[str] = []
            for field in visible_fields:
                option_label = _field_label(field)
                options.append(option_label or field.get_attribute("value") or "")
            required = any(_is_required(field, label) for field in visible_fields)
            answer = context.resolver.resolve(
                FormQuestion(label=label, field_type="radio", options=options, required=required)
            )
            if answer is None:
                if required:
                    unresolved.append(label or name)
                continue
            selected = next(
                (
                    field
                    for field, option in zip(visible_fields, options, strict=True)
                    if answer.value.lower() in option.lower()
                    or option.lower() in answer.value.lower()
                ),
                None,
            )
            if selected is not None:
                selected.check()
            elif required:
                unresolved.append(label or name)

    def _fill_checkboxes(
        self,
        page: Page,
        unresolved: list[str],
    ) -> None:
        fields = page.locator('input[type="checkbox"]')
        attestations = ("agree", "acknowledge", "certify", "privacy", "terms", "consent")
        for index in range(fields.count()):
            field = fields.nth(index)
            if not _visible(field) or field.is_checked():
                continue
            label = _field_label(field)
            if not _is_required(field, label):
                continue
            if any(word in label.lower() for word in attestations):
                field.check()
            else:
                unresolved.append(label or field.get_attribute("name") or "Required checkbox")

    def _fill_comboboxes(
        self,
        page: Page,
        context: ApplicationContext,
        unresolved: list[str],
    ) -> None:
        fields = page.locator('[role="combobox"]:not(select)')
        for index in range(fields.count()):
            field = fields.nth(index)
            if not _visible(field):
                continue
            try:
                current = field.input_value()
            except Exception:
                current = field.inner_text()
            if current.strip():
                continue
            label = _field_label(field)
            required = _is_required(field, label)
            try:
                field.click()
                page.wait_for_timeout(150)
                option_locator = page.locator('[role="option"]:visible')
                options = [
                    value.strip() for value in option_locator.all_inner_texts() if value.strip()
                ]
            except Exception:
                options = []
            answer = context.resolver.resolve(
                FormQuestion(
                    label=label,
                    field_type="select",
                    options=options,
                    required=required,
                )
            )
            if answer is not None and options:
                option = page.get_by_role("option", name=answer.value, exact=True)
                if not option.count():
                    option = page.get_by_role("option", name=answer.value, exact=False)
                if option.count():
                    option.first.click()
                    continue
            with suppress(Exception):
                page.keyboard.press("Escape")
            if required:
                unresolved.append(label or "Required combobox")

    def prefill(
        self,
        page: Page,
        context: ApplicationContext,
        *,
        ignore_captcha: bool = False,
    ) -> list[str]:
        if not ignore_captcha and (captcha := detect_captcha(page)):
            return [f"{captcha} requires manual completion"]
        unresolved: list[str] = []
        self._fill_files(page, context, unresolved)
        self._fill_text_fields(page, context, unresolved)
        self._fill_selects(page, context, unresolved)
        self._fill_radios(page, context, unresolved)
        self._fill_checkboxes(page, unresolved)
        self._fill_comboboxes(page, context, unresolved)
        return list(dict.fromkeys(value for value in unresolved if value))

    def _first_submit(self, page: Page) -> Locator | None:
        for selector in self.submit_selectors:
            candidates = page.locator(selector)
            for index in range(candidates.count()):
                candidate = candidates.nth(index)
                if _visible(candidate):
                    return candidate
        return None

    def _confirmed(self, page: Page) -> bool:
        try:
            text = page.locator("body").inner_text(timeout=5_000).lower()
        except Exception:
            text = ""
        url = page.url.lower()
        return any(pattern in text for pattern in self.confirmation_patterns) or any(
            token in url for token in ("confirmation", "thank-you", "thank_you", "submitted")
        )

    def _validation_error(self, page: Page) -> str | None:
        selectors = (
            '[role="alert"]',
            ".field-error",
            ".error-message",
            ".application--error",
            '[aria-invalid="true"]',
        )
        messages: list[str] = []
        for selector in selectors:
            locator = page.locator(selector)
            for index in range(min(locator.count(), 8)):
                try:
                    text = locator.nth(index).inner_text().strip()
                except Exception:
                    text = ""
                if text and text not in messages:
                    messages.append(text)
        return "; ".join(messages)[:1000] if messages else None

    @staticmethod
    def _artifact_paths(context: ApplicationContext) -> tuple[Path, Path]:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        stem = f"{_slug(context.job.company)}-{context.job.id[:8]}-{stamp}"
        context.screenshot_dir.mkdir(parents=True, exist_ok=True)
        context.trace_dir.mkdir(parents=True, exist_ok=True)
        return context.screenshot_dir / f"{stem}.png", context.trace_dir / f"{stem}.zip"

    def apply(
        self,
        browser: Browser,
        context: ApplicationContext,
        *,
        dry_run: bool = False,
    ) -> ApplyResult:
        screenshot_path, trace_candidate = self._artifact_paths(context)
        trace_path: Path | None = trace_candidate
        browser_context = browser.new_context()
        browser_context.set_default_timeout(context.navigation_timeout_ms)
        browser_context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = browser_context.new_page()
        result = ApplyResult("failed", "Application attempt failed")
        try:
            page.goto(
                context.job.url,
                wait_until="domcontentloaded",
                timeout=context.navigation_timeout_ms,
            )
            self.dismiss_cookie_banners(page)
            self.prepare_page(page)
            if self._page_is_closed(page):
                result = ApplyResult("skipped", "Listing is closed")
            elif captcha := detect_captcha(page):
                result = ApplyResult("needs_review", f"{captcha} requires manual completion")
            elif not self._application_form_present(page):
                result = ApplyResult("needs_review", "Visible application form was not found")
            else:
                unresolved = self.prefill(page, context)
                page.screenshot(path=str(screenshot_path), full_page=True)
                if unresolved:
                    result = ApplyResult(
                        "needs_review",
                        "Unresolved required fields: " + ", ".join(unresolved),
                    )
                elif dry_run:
                    result = ApplyResult("queued", "Dry run completed; submit was not clicked")
                elif captcha := detect_captcha(page):
                    result = ApplyResult("needs_review", f"{captcha} requires manual completion")
                else:
                    submit = self._first_submit(page)
                    if submit is None:
                        result = ApplyResult("needs_review", "Could not find the submit button")
                    else:
                        submit.click(timeout=10_000)
                        with suppress(PlaywrightTimeoutError):
                            page.wait_for_load_state("domcontentloaded", timeout=10_000)
                        page.wait_for_timeout(1_000)
                        page.screenshot(path=str(screenshot_path), full_page=True)
                        if self._confirmed(page):
                            result = ApplyResult("applied", "Application submitted")
                        elif captcha := detect_captcha(page):
                            result = ApplyResult(
                                "needs_review",
                                f"{captcha} requires manual completion",
                            )
                        else:
                            validation = self._validation_error(page)
                            result = ApplyResult(
                                "needs_review",
                                validation or "Submission confirmation was not detected",
                            )
        except Exception as error:
            with suppress(Exception):
                page.screenshot(path=str(screenshot_path), full_page=True)
            result = ApplyResult("failed", f"{type(error).__name__}: {error}")
        finally:
            if not screenshot_path.exists():
                with suppress(Exception):
                    page.screenshot(path=str(screenshot_path), full_page=True)
            try:
                browser_context.tracing.stop(path=str(trace_path))
            except Exception:
                trace_path = None
            browser_context.close()
        result.screenshot_path = screenshot_path if screenshot_path.exists() else None
        result.trace_path = trace_path if trace_path and trace_path.exists() else None
        return result
