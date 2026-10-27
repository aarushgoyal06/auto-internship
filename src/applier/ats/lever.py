from __future__ import annotations

from playwright.sync_api import Page

from applier.ats.base import BaseATSAdapter


class LeverAdapter(BaseATSAdapter):
    name = "lever"
    form_selectors = (".application-form", 'form[action*="/apply"]')
    submit_selectors = (
        'button[type="submit"]',
        'input[type="submit"]',
        ".application-submit button",
    )
    confirmation_patterns = BaseATSAdapter.confirmation_patterns + (
        "application submitted",
        "your application was sent",
    )

    def prepare_page(self, page: Page) -> None:
        if page.locator(".application-form, form.application").count():
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
