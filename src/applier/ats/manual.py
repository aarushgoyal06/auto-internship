from __future__ import annotations

from playwright.sync_api import Browser

from applier.ats.base import ApplicationContext, ApplyResult, BaseATSAdapter


class ManualReviewAdapter(BaseATSAdapter):
    name = "manual"

    def __init__(self, ats_name: str) -> None:
        self.ats_name = ats_name

    def apply(
        self,
        browser: Browser,
        context: ApplicationContext,
        *,
        dry_run: bool = False,
    ) -> ApplyResult:
        return ApplyResult(
            "needs_review",
            f"{self.ats_name} does not have an automatic adapter yet",
        )
