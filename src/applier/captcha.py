from __future__ import annotations

from playwright.sync_api import Page

CAPTCHA_SELECTORS = {
    "reCAPTCHA": (
        'iframe[src*="recaptcha"][src*="bframe"]',
        'iframe[title*="recaptcha challenge" i]',
        '.g-recaptcha:not([data-size="invisible"])',
    ),
    "hCaptcha": (
        'iframe[src*="hcaptcha.com"][title*="challenge" i]',
        '.h-captcha:not([data-size="invisible"])',
    ),
    "Cloudflare Turnstile": (
        'iframe[src*="challenges.cloudflare.com"]',
        '.cf-turnstile:not([data-size="invisible"])',
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
