import pytest

from applier.config import LLMConfig
from applier.llm import (
    AnswerContext,
    LLMError,
    OllamaProvider,
    build_provider,
    generate_application_answer,
)


class LongProvider:
    def complete(self, _prompt: str) -> str:
        return " ".join(["truthful"] * 100)


def test_uses_configured_fallback_when_primary_key_is_missing(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    config = LLMConfig(
        provider="gemini",
        model="gemini-2.5-flash",
        fallback_provider="ollama",
        fallback_model="llama3.2",
    )
    provider = build_provider(config)
    assert isinstance(provider, OllamaProvider)
    assert provider.model == "llama3.2"


def test_reports_both_missing_api_keys(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(LLMError, match="GEMINI_API_KEY.*GROQ_API_KEY"):
        build_provider(LLMConfig())


def test_generated_answer_respects_character_limit():
    answer = generate_application_answer(
        LongProvider(),
        "Why this company?",
        AnswerContext(
            company="Acme",
            role="Intern",
            job_description="Build software.",
            resume_facts="The candidate writes Python.",
        ),
        max_chars=80,
    )
    assert len(answer) <= 80
    assert answer.endswith(".")
