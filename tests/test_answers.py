from applier.answers import AnswerResolver, FormQuestion
from applier.llm import AnswerContext


class FakeProvider:
    def complete(self, _prompt: str) -> str:
        return "I am interested in applying my Python and AWS experience to this role."


def _resolver(profile, provider=None):
    return AnswerResolver(
        profile,
        AnswerContext(
            company="Acme",
            role="Software Engineer Intern",
            job_description="Build Python services on AWS.",
            resume_facts=profile.factual_context(),
        ),
        provider,
    )


def test_resolves_identity_authorization_and_eeo(profile):
    resolver = _resolver(profile)
    assert resolver.resolve(FormQuestion("First name")).value == "Aarush"
    assert resolver.resolve(FormQuestion("Email address")).value == "aarushg@udel.edu"
    assert (
        resolver.resolve(
            FormQuestion(
                "Are you legally authorized to work in the United States?",
                field_type="select",
                options=["Select", "Yes", "No"],
            )
        ).value
        == "Yes"
    )
    assert (
        resolver.resolve(
            FormQuestion(
                "Will you now or in the future require sponsorship?",
                field_type="radio",
                options=["Yes", "No"],
            )
        ).value
        == "No"
    )
    assert (
        resolver.resolve(
            FormQuestion(
                "Gender",
                field_type="select",
                options=["Woman", "Man", "Prefer not to say"],
            )
        ).value
        == "Prefer not to say"
    )


def test_missing_profile_fact_is_not_invented(profile):
    resolver = _resolver(profile)
    assert resolver.resolve(FormQuestion("Street address", required=True)) is None
    assert resolver.resolve(FormQuestion("Salary expectation", required=True)) is None
    assert resolver.resolve(FormQuestion("Cover letter", field_type="textarea")) is None


def test_generates_and_audits_custom_answer(profile):
    resolver = _resolver(profile, FakeProvider())
    answer = resolver.resolve(
        FormQuestion("Why do you want to work at Acme?", field_type="textarea", max_length=200)
    )
    assert answer is not None
    assert answer.source == "llm"
    assert "Python and AWS" in answer.value
    assert resolver.audit["Why do you want to work at Acme?"]["source"] == "llm"
