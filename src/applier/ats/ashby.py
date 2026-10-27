from __future__ import annotations

from playwright.sync_api import Page

from applier.ats.base import BaseATSAdapter


class AshbyAdapter(BaseATSAdapter):
    name = "ashby"
    form_selectors = (
        'form[data-testid="application-form"]',
        'form[action*="application"]',
        "form",
    )
    submit_selectors = (
        'button[type="submit"]',
        'button:has-text("Submit Application")',
        'button:has-text("Submit")',
    )
    confirmation_patterns = BaseATSAdapter.confirmation_patterns + (
        "application received",
        "we received your application",
    )

    def prepare_page(self, page: Page) -> None:
        if "/application" in page.url or page.locator('input[type="file"]').count():
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
