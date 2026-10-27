from __future__ import annotations

import re
from dataclasses import dataclass, field

from applier.config import Profile
from applier.llm import AnswerContext, LLMError, LLMProvider, generate_application_answer


@dataclass(slots=True)
class FormQuestion:
    label: str
    field_type: str = "text"
    options: list[str] = field(default_factory=list)
    required: bool = False
    max_length: int = 1000


@dataclass(slots=True)
class ResolvedAnswer:
    value: str
    source: str


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _select_option(options: list[str], candidates: list[str]) -> str | None:
    normalized = [(_normalize(option), option) for option in options]
    for candidate in candidates:
        wanted = _normalize(candidate)
        for option_key, original in normalized:
            if option_key == wanted:
                return original
        for option_key, original in normalized:
            if wanted in option_key or option_key in wanted:
                return original
    return None


def _boolean_option(options: list[str], value: bool) -> str:
    if not options:
        return "Yes" if value else "No"
    selected = _select_option(options, ["Yes" if value else "No"])
    if selected is None:
        raise ValueError("No matching yes/no option")
    return selected


class AnswerResolver:
    def __init__(
        self,
        profile: Profile,
        context: AnswerContext,
        provider: LLMProvider | None = None,
    ) -> None:
        self.profile = profile
        self.context = context
        self.provider = provider
        self.audit: dict[str, dict[str, str]] = {}

    def _record(self, question: FormQuestion, value: str, source: str) -> ResolvedAnswer:
        self.audit[question.label] = {"answer": value, "source": source}
        return ResolvedAnswer(value, source)

    def _option_or_value(
        self,
        question: FormQuestion,
        candidates: list[str],
        source: str = "profile",
    ) -> ResolvedAnswer | None:
        if question.options:
            value = _select_option(question.options, candidates)
            return self._record(question, value, source) if value is not None else None
        return self._record(question, candidates[0], source)

    def resolve(self, question: FormQuestion) -> ResolvedAnswer | None:
        label = _normalize(question.label)
        personal = self.profile.personal
        education = self.profile.education
        authorization = self.profile.authorization
        preferences = self.profile.preferences

        if not label:
            return None
        if "first name" in label or label in {"given name", "first"}:
            return self._record(question, personal.first_name, "profile")
        if "last name" in label or "family name" in label or label in {"surname", "last"}:
            return self._record(question, personal.last_name, "profile")
        if label in {"name", "full name", "legal name"} or "full legal name" in label:
            return self._record(question, personal.full_name, "profile")
        if "email" in label:
            return self._record(question, personal.email, "profile")
        if any(token in label for token in ("phone", "mobile number", "telephone")):
            return self._record(question, personal.phone, "profile")
        if "linkedin" in label:
            return self._record(question, personal.linkedin, "profile")
        if "github" in label:
            return self._record(question, personal.github, "profile")
        if any(token in label for token in ("website", "portfolio", "personal site")):
            return self._record(question, personal.website, "profile")

        if any(token in label for token in ("street address", "address line 1", "address 1")):
            if personal.address_line_1:
                return self._record(question, personal.address_line_1, "profile")
            return None
        if label in {"city", "current city"} or "city of residence" in label:
            return self._record(question, personal.city, "profile") if personal.city else None
        if label in {"state", "province", "state province"} or "state of residence" in label:
            if not personal.state:
                return None
            return self._option_or_value(question, [personal.state])
        if any(token in label for token in ("zip", "postal code")):
            return self._record(question, personal.postal_code, "profile") if personal.postal_code else None
        if label in {"country", "country of residence", "current country"}:
            return self._option_or_value(
                question,
                ["United States", "United States of America", "USA", "US"],
            )

        if any(token in label for token in ("school", "university", "college")):
            return self._option_or_value(question, [education.school])
        if "gpa" in label or "grade point average" in label:
            return self._record(question, education.gpa, "profile")
        if "graduation year" in label or "year of graduation" in label:
            return self._option_or_value(question, [str(education.graduation_year)])
        if "graduation month" in label:
            return self._option_or_value(question, [education.graduation_month])
        if "graduation date" in label or "expected graduation" in label:
            return self._record(
                question,
                f"{education.graduation_month} {education.graduation_year}",
                "profile",
            )
        if "degree" in label:
            return self._option_or_value(
                question,
                [education.degree, "Bachelor's Degree", "Bachelors", "Bachelor"],
            )
        if any(token in label for token in ("major", "field of study", "discipline")):
            return self._option_or_value(question, education.majors)

        if "country of citizenship" in label or "citizenship country" in label:
            return self._option_or_value(
                question,
                ["United States", "United States of America", "USA", "US"],
            )
        if "citizen" in label:
            return self._record(
                question,
                _boolean_option(question.options, authorization.us_citizen),
                "profile",
            )
        if any(
            token in label
            for token in ("authorized to work", "work authorization", "legally authorized")
        ):
            return self._record(
                question,
                _boolean_option(question.options, authorization.authorized_to_work_us),
                "profile",
            )
        if any(
            token in label
            for token in ("sponsorship", "sponsor", "immigration support", "visa support")
        ):
            return self._record(
                question,
                _boolean_option(question.options, authorization.requires_sponsorship),
                "profile",
            )
        if "relocat" in label:
            return self._record(
                question,
                _boolean_option(question.options, preferences.willing_to_relocate),
                "profile",
            )

        eeo_terms = (
            "gender",
            "sex",
            "race",
            "ethnicity",
            "veteran",
            "disability",
            "pronoun",
            "sexual orientation",
            "transgender",
            "hispanic",
        )
        if any(term in label for term in eeo_terms):
            preferred = _select_option(
                question.options,
                [
                    "Prefer not to say",
                    "Prefer not to answer",
                    "Decline to self-identify",
                    "I do not wish to answer",
                    "Choose not to disclose",
                    "Not specified",
                ],
            )
            return self._record(question, preferred, "eeo_default") if preferred else None

        if any(token in label for token in ("how did you hear", "source")):
            return self._option_or_value(
                question,
                ["Simplify", "Job board", "Online job board", "Other"],
                source="fixed",
            )
        if "cover letter" in label:
            return None
        if any(token in label for token in ("salary", "compensation", "pay expectation")):
            return None
        if any(token in label for token in ("start date", "available to start", "availability")):
            if preferences.availability:
                return self._record(question, preferences.availability, "profile")
            return None

        if question.field_type in {"text", "textarea"} and self.provider is not None:
            try:
                value = generate_application_answer(
                    self.provider,
                    question.label,
                    self.context,
                    max_chars=question.max_length,
                )
            except LLMError:
                return None
            return self._record(question, value, "llm")
        return None
