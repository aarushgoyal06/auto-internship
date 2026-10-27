from __future__ import annotations

from playwright.sync_api import Page

from applier.answers import FormQuestion
from applier.ats.base import ApplicationContext, BaseATSAdapter


class AshbyAdapter(BaseATSAdapter):
    name = "ashby"
    form_selectors = (
        ".ashby-application-form-container",
        'form[data-testid="application-form"]',
        'form[action*="application"]',
    )
    submit_selectors = (
        ".ashby-application-form-submit-button",
        'button[type="submit"]',
        'button:has-text("Submit Application")',
        'button:has-text("Submit")',
    )
    confirmation_patterns = BaseATSAdapter.confirmation_patterns + (
        "application received",
        "we received your application",
    )

    def prepare_page(self, page: Page) -> None:
        if page.locator(".ashby-application-form-container").count():
            return
        if "/application" in page.url and page.locator('input[type="file"]').count():
            return
        for locator in (
            page.locator('a[href*="/application"]'),
            page.get_by_role("link", name="Apply", exact=False),
            page.get_by_role("button", name="Apply", exact=False),
        ):
            try:
                if locator.count() and locator.first.is_visible():
                    locator.first.click()
                    page.wait_for_load_state("domcontentloaded")
                    return
            except Exception:
                continue

    def prefill(
        self,
        page: Page,
        context: ApplicationContext,
        *,
        ignore_captcha: bool = False,
    ) -> list[str]:
        unresolved = super().prefill(page, context, ignore_captcha=ignore_captcha)
        entries = page.locator(".ashby-application-form-field-entry")
        for index in range(entries.count()):
            entry = entries.nth(index)
            options_locator = entry.locator(".ashby-application-form-input-yesno-option")
            if not options_locator.count():
                continue
            title = entry.locator(".ashby-application-form-question-title")
            label = title.first.inner_text().strip() if title.count() else ""
            options: list[str] = []
            for option_index in range(options_locator.count()):
                option = options_locator.nth(option_index)
                options.append(
                    (option.get_attribute("data-option") or option.inner_text() or "").strip()
                )
            required = entry.locator(
                '[aria-required="true"], [required]'
            ).count() > 0 or label.endswith("*")
            answer = context.resolver.resolve(
                FormQuestion(
                    label=label.rstrip("*").strip(),
                    field_type="radio",
                    options=options,
                    required=required,
                )
            )
            if answer is None:
                if required:
                    unresolved.append(label or "Required yes/no question")
                continue
            for option_index, option_value in enumerate(options):
                if answer.value.casefold() == option_value.casefold():
                    options_locator.nth(option_index).click()
                    break
            else:
                if required:
                    unresolved.append(label or "Required yes/no question")
        return list(dict.fromkeys(unresolved))

    def _confirmed(self, page: Page) -> bool:
        success = page.locator(".ashby-application-form-success-container")
        return bool(success.count() and success.first.is_visible())

    def _validation_error(self, page: Page) -> str | None:
        base_error = super()._validation_error(page)
        if base_error:
            return base_error
        failure = page.locator(".ashby-application-form-failure-container")
        if failure.count() and failure.first.is_visible():
            return failure.first.inner_text().strip()[:1000]
        return None
