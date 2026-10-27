from __future__ import annotations

from playwright.sync_api import Page

CAPTCHA_SELECTORS = {
    "reCAPTCHA": (
        'iframe[src*="recaptcha"]',
        ".g-recaptcha",
        'textarea[name="g-recaptcha-response"]',
    ),
    "hCaptcha": (
        'iframe[src*="hcaptcha.com"]',
        ".h-captcha",
        'textarea[name="h-captcha-response"]',
    ),
    "Cloudflare Turnstile": (
        'iframe[src*="challenges.cloudflare.com"]',
        ".cf-turnstile",
        'input[name="cf-turnstile-response"]',
    ),
}


def detect_captcha(page: Page) -> str | None:
    for name, selectors in CAPTCHA_SELECTORS.items():
        for selector in selectors:
            try:
                matches = page.locator(selector)
                for index in range(matches.count()):
                    if matches.nth(index).is_visible():
                        return name
            except Exception:
                continue
    return None
