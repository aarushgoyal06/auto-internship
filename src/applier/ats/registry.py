from __future__ import annotations

from applier.ats.ashby import AshbyAdapter
from applier.ats.base import BaseATSAdapter
from applier.ats.greenhouse import GreenhouseAdapter
from applier.ats.lever import LeverAdapter
from applier.ats.manual import ManualReviewAdapter
from applier.ats.workday import WorkdayAdapter


def adapter_for(ats_type: str) -> BaseATSAdapter:
    adapters: dict[str, BaseATSAdapter] = {
        "greenhouse": GreenhouseAdapter(),
        "lever": LeverAdapter(),
        "ashby": AshbyAdapter(),
        "workday": WorkdayAdapter(),
    }
    return adapters.get(ats_type, ManualReviewAdapter(ats_type.title() or "Unknown ATS"))
