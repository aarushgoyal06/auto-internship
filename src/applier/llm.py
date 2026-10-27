from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Protocol

import httpx
from google import genai
from groq import Groq

from applier.config import LLMConfig, Profile


class LLMError(RuntimeError):
    """Raised when an LLM provider cannot generate a safe answer."""


@dataclass(slots=True)
class AnswerContext:
    company: str
    role: str
    job_description: str
    resume_facts: str


class LLMProvider(Protocol):
    def complete(self, prompt: str) -> str: ...


class GeminiProvider:
    def __init__(self, api_key: str, model: str) -> None:
        self.client = genai.Client(api_key=api_key)
        self.model = model

    def complete(self, prompt: str) -> str:
        response = self.client.models.generate_content(model=self.model, contents=prompt)
        if not response.text:
            raise LLMError("Gemini returned an empty response")
        return response.text.strip()


class GroqProvider:
    def __init__(self, api_key: str, model: str) -> None:
        self.client = Groq(api_key=api_key)
        self.model = model

    def complete(self, prompt: str) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        content = response.choices[0].message.content
        if not content:
            raise LLMError("Groq returned an empty response")
        return content.strip()


class OllamaProvider:
    def __init__(self, base_url: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model

    def complete(self, prompt: str) -> str:
        response = httpx.post(
            f"{self.base_url}/api/generate",
            json={"model": self.model, "prompt": prompt, "stream": False},
            timeout=120,
        )
        response.raise_for_status()
        content = response.json().get("response")
        if not content:
            raise LLMError("Ollama returned an empty response")
        return str(content).strip()


class FallbackProvider:
    def __init__(self, primary: LLMProvider, fallback: LLMProvider) -> None:
        self.primary = primary
        self.fallback = fallback

    def complete(self, prompt: str) -> str:
        try:
            return self.primary.complete(prompt)
        except Exception as primary_error:
            try:
                return self.fallback.complete(prompt)
            except Exception as fallback_error:
                raise LLMError(
                    f"Primary and fallback LLMs failed: {primary_error}; {fallback_error}"
                ) from fallback_error


def _single_provider(
    provider: str,
    model: str,
    config: LLMConfig,
) -> LLMProvider:
    if provider == "gemini":
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise LLMError("GEMINI_API_KEY is not configured")
        return GeminiProvider(key, model)
    if provider == "groq":
        key = os.getenv("GROQ_API_KEY")
        if not key:
            raise LLMError("GROQ_API_KEY is not configured")
        return GroqProvider(key, model)
    if provider == "ollama":
        return OllamaProvider(config.ollama_url, model or config.ollama_model)
    raise LLMError(f"Unsupported LLM provider: {provider}")


def build_provider(config: LLMConfig) -> LLMProvider:
    primary = _single_provider(config.provider, config.model, config)
    if not config.fallback_provider or not config.fallback_model:
        return primary
    try:
        fallback = _single_provider(
            config.fallback_provider,
            config.fallback_model,
            config,
        )
    except LLMError:
        return primary
    return FallbackProvider(primary, fallback)


def _trim_to_limit(text: str, max_chars: int) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip().strip('"')
    if len(cleaned) <= max_chars:
        return cleaned
    clipped = cleaned[: max_chars + 1]
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return clipped.rstrip(" ,;:-") + "."


def generate_application_answer(
    provider: LLMProvider,
    question: str,
    context: AnswerContext,
    *,
    max_chars: int = 1000,
) -> str:
    prompt = f"""Write a truthful first-person answer to a job application question.

Hard rules:
- Use only the candidate facts below.
- Never invent a project, skill, metric, preference, or experience.
- If the facts do not support the question, say so plainly instead of guessing.
- Be specific and concise. Do not use hype, salutations, headings, or markdown.
- Do not mention that you are an AI.
- Stay under {max_chars} characters.

Company: {context.company}
Role: {context.role}
Question: {question}

Candidate facts:
{context.resume_facts}

Job description:
{context.job_description[:12_000]}

Return only the answer."""
    return _trim_to_limit(provider.complete(prompt), max_chars)


def extract_llm_keywords(
    provider: LLMProvider,
    job_description: str,
    profile: Profile,
    *,
    limit: int = 10,
) -> list[str]:
    known_skills = sorted({skill for values in profile.skills.values() for skill in values})
    prompt = f"""Select up to {limit} skills that are both important in the job description and
truthfully present in the candidate's known skill list. Never add another skill.
Return only a JSON array of strings.

Known skills: {json.dumps(known_skills)}
Job description:
{job_description[:12_000]}"""
    response = provider.complete(prompt)
    match = re.search(r"\[[\s\S]*\]", response)
    if not match:
        raise LLMError("Keyword response did not contain a JSON array")
    value = json.loads(match.group(0))
    if not isinstance(value, list):
        raise LLMError("Keyword response was not a list")
    known = {skill.lower(): skill.lower() for skill in known_skills}
    output: list[str] = []
    for item in value:
        normalized = str(item).strip().lower()
        if normalized in known and normalized not in output:
            output.append(normalized)
    return output[:limit]
