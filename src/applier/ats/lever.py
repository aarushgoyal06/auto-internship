from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Page

from applier.ats.base import BaseATSAdapter


class LeverAdapter(BaseATSAdapter):
    name = "lever"
    form_selectors = (
        "form#application-form",
        ".application-form",
        'form[action*="/apply"]',
    )
    submit_selectors = (
        '#btn-submit[data-qa="btn-submit"]',
        "#btn-submit",
        ".template-btn-submit",
        'button[type="submit"]',
        'input[type="submit"]',
        ".application-submit button",
    )
    confirmation_patterns = BaseATSAdapter.confirmation_patterns + (
        "application submitted",
        "your application was sent",
    )

    def prepare_page(self, page: Page) -> None:
        if page.locator("form#application-form, .application-form, form.application").count():
            return
        for locator in (
            page.locator('a[href$="/apply"]'),
            page.get_by_role("link", name="Apply for this job", exact=False),
            page.get_by_role("button", name="Apply", exact=False),
        ):
            try:
                if locator.count() and locator.first.is_visible():
                    locator.first.click()
                    page.wait_for_load_state("domcontentloaded")
                    return
            except Exception:
                continue

    def _confirmed(self, page: Page) -> bool:
        parsed = urlparse(page.url)
        if parsed.path.rstrip("/").endswith("/thanks"):
            return True
        if "LeverAppId" in parse_qs(parsed.query):
            return True
        for selector in (
            ".application-confirmation",
            '[data-qa="application-confirmation"]',
            ".confirmation",
        ):
            locator = page.locator(selector)
            if locator.count() and locator.first.is_visible():
                return True
        return False
