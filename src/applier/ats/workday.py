from applier.ats.manual import ManualReviewAdapter


class WorkdayAdapter(ManualReviewAdapter):
    name = "workday"

    def __init__(self) -> None:
        super().__init__("Workday")
