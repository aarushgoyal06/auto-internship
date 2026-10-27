from __future__ import annotations

from playwright.sync_api import Page

from applier.ats.base import BaseATSAdapter


class GreenhouseAdapter(BaseATSAdapter):
    name = "greenhouse"
    form_selectors = (
        "#application-form",
        "#application_form",
        'form[action*="applications"]',
    )
    submit_selectors = (
        '#application-form button[type="submit"]',
        '#application_form input[type="submit"]',
        'button[type="submit"]',
        'input[type="submit"]',
    )
    confirmation_patterns = BaseATSAdapter.confirmation_patterns + (
        "your application has been received",
        "we have received your application",
    )

    def prepare_page(self, page: Page) -> None:
        if page.locator(
            '#application-form, #application_form, input[name="first_name"]'
        ).count():
            return
        for locator in (
            page.get_by_role("link", name="Apply", exact=True),
            page.get_by_role("button", name="Apply", exact=True),
            page.locator('a[href*="#app"]'),
        ):
            try:
                if locator.count() and locator.first.is_visible():
                    locator.first.click()
                    page.wait_for_timeout(500)
                    return
            except Exception:
                continue
