from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator


class PersonalProfile(BaseModel):
    first_name: str
    last_name: str
    email: str
    phone: str
    address_line_1: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str = "United States"
    linkedin: str
    github: str
    website: str

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class EducationProfile(BaseModel):
    school: str
    degree: str
    majors: list[str]
    minor: str | None = None
    graduation_month: str
    graduation_year: int
    gpa: str


class AuthorizationProfile(BaseModel):
    us_citizen: bool
    authorized_to_work_us: bool
    requires_sponsorship: bool


class PreferenceProfile(BaseModel):
    willing_to_relocate: bool = True
    availability: str | None = None
    cover_letter: bool = False
    eeo_default: str = "prefer_not_to_say"


class Profile(BaseModel):
    personal: PersonalProfile
    education: EducationProfile
    authorization: AuthorizationProfile
    preferences: PreferenceProfile
    skills: dict[str, list[str]]

    def factual_context(self) -> str:
        skills = "; ".join(f"{group}: {', '.join(items)}" for group, items in self.skills.items())
        majors = " and ".join(self.education.majors)
        return (
            f"{self.personal.full_name} studies {majors} at {self.education.school}, "
            f"graduates {self.education.graduation_month} {self.education.graduation_year}, "
            f"and has a {self.education.gpa} GPA. Skills: {skills}. "
            "The candidate is a US citizen, is authorized to work in the United States, "
            "and does not require sponsorship."
        )


class SourceConfig(BaseModel):
    url: str
    categories: list[str] = Field(default_factory=lambda: ["Software", "Software Engineering"])
    term: str = "Summer 2027"
    us_only: bool = True


class RuntimeConfig(BaseModel):
    headless: bool = True
    min_delay_seconds: float = 20
    max_delay_seconds: float = 60
    max_retries: int = 2
    max_applications: int | None = None
    navigation_timeout_ms: int = 45_000
    slow_mo_ms: int = 0

    @model_validator(mode="after")
    def validate_delay(self) -> RuntimeConfig:
        if self.min_delay_seconds < 0 or self.max_delay_seconds < self.min_delay_seconds:
            raise ValueError("runtime delay range is invalid")
        return self


class LLMConfig(BaseModel):
    provider: str = "gemini"
    model: str = "gemini-2.5-flash"
    fallback_provider: str | None = "groq"
    fallback_model: str | None = "llama-3.3-70b-versatile"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.2"


class PathConfig(BaseModel):
    database: Path = Path("data/applier.db")
    resumes: Path = Path("data/resumes")
    screenshots: Path = Path("data/screenshots")
    traces: Path = Path("data/traces")
    gmail_token: Path = Path("data/gmail_token.json")
    gmail_credentials: Path = Path("credentials.json")


class Settings(BaseModel):
    source: SourceConfig
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    paths: PathConfig = Field(default_factory=PathConfig)

    def ensure_runtime_dirs(self) -> None:
        for path in (
            self.paths.database.parent,
            self.paths.resumes,
            self.paths.screenshots,
            self.paths.traces,
            self.paths.gmail_token.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    with path.open(encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a YAML mapping in {path}")
    return value


def load_profile(path: Path | None = None) -> Profile:
    load_dotenv()
    selected = path or Path(os.getenv("APPLIER_PROFILE", "config/profile.yaml"))
    return Profile.model_validate(_load_yaml(selected))


def load_settings(path: Path | None = None) -> Settings:
    load_dotenv()
    selected = path or Path(os.getenv("APPLIER_SETTINGS", "config/settings.yaml"))
    settings = Settings.model_validate(_load_yaml(selected))
    if override := os.getenv("APPLIER_DATABASE"):
        settings.paths.database = Path(override)
    return settings
