from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pypdf import PdfReader

from applier.config import Profile
from applier.db import Job

COMMON_TECH_KEYWORDS = {
    "api",
    "backend",
    "cloud",
    "ci/cd",
    "cybersecurity",
    "data",
    "database",
    "devops",
    "distributed systems",
    "frontend",
    "full stack",
    "infrastructure",
    "machine learning",
    "mobile",
    "network",
    "object oriented",
    "payments",
    "rest",
    "security",
    "sql",
    "testing",
    "unit tests",
}

SKILL_ALIASES = {
    "aws": ("amazon web services",),
    "lambda": ("aws lambda",),
    "node.js": ("node", "nodejs"),
    "postgresql": ("postgres", "sql"),
    "javascript": ("js",),
    "typescript": ("ts",),
    "terraform": ("infrastructure as code", "iac"),
    "ci/cd": ("continuous integration", "continuous delivery", "continuous deployment"),
}

LATEX_ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


class ResumeCompileError(RuntimeError):
    """Raised when a LaTeX resume cannot be compiled."""


@dataclass(slots=True)
class TailoredResume:
    pdf_path: Path
    tex_path: Path
    keywords: list[str]
    page_count: int
    cache_hit: bool = False


def latex_escape(value: str) -> str:
    return "".join(LATEX_ESCAPES.get(char, char) for char in value)


def _term_present(text: str, term: str) -> bool:
    escaped = re.escape(term)
    if term.isalnum():
        return re.search(rf"\b{escaped}\b", text, flags=re.IGNORECASE) is not None
    return re.search(escaped, text, flags=re.IGNORECASE) is not None


def extract_keywords(jd_text: str, profile: Profile, resume_data: dict[str, Any]) -> list[str]:
    candidates = set(COMMON_TECH_KEYWORDS)
    for skills in profile.skills.values():
        candidates.update(skill.lower() for skill in skills)
    for role in resume_data.get("experience", []):
        for bullet in role.get("bullets", []):
            candidates.update(str(keyword).lower() for keyword in bullet.get("keywords", []))
        for subrole in role.get("subroles", []):
            for bullet in subrole.get("bullets", []):
                candidates.update(str(keyword).lower() for keyword in bullet.get("keywords", []))

    lowered = jd_text.lower()
    scores: list[tuple[int, str]] = []
    for candidate in candidates:
        variants = (candidate, *SKILL_ALIASES.get(candidate, ()))
        count = sum(len(re.findall(re.escape(variant), lowered)) for variant in variants)
        if count:
            scores.append((count, candidate))
    scores.sort(key=lambda item: (-item[0], -len(item[1]), item[1]))
    return [keyword for _, keyword in scores[:20]]


def bold_matching_terms(text: str, keywords: list[str]) -> str:
    terms = sorted(
        {term for term in keywords if len(term) > 1 and _term_present(text, term)},
        key=len,
        reverse=True,
    )
    if not terms:
        return latex_escape(text)
    pattern = re.compile("|".join(re.escape(term) for term in terms), re.IGNORECASE)
    output: list[str] = []
    cursor = 0
    for match in pattern.finditer(text):
        output.append(latex_escape(text[cursor : match.start()]))
        output.append(r"\textbf{" + latex_escape(match.group(0)) + "}")
        cursor = match.end()
    output.append(latex_escape(text[cursor:]))
    return "".join(output)


class ResumeTailor:
    def __init__(
        self,
        *,
        template_path: Path = Path("resume/base.tex.j2"),
        bullets_path: Path = Path("resume/bullets.yaml"),
        output_dir: Path = Path("data/resumes"),
    ) -> None:
        self.template_path = template_path
        self.bullets_path = bullets_path
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.environment = Environment(
            loader=FileSystemLoader(str(template_path.parent)),
            undefined=StrictUndefined,
            autoescape=False,
            variable_start_string="(((",
            variable_end_string=")))",
            block_start_string="<%",
            block_end_string="%>",
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def _load_resume_data(self) -> dict[str, Any]:
        with self.bullets_path.open(encoding="utf-8") as handle:
            value = yaml.safe_load(handle)
        if not isinstance(value, dict):
            raise ValueError(f"Expected a YAML mapping in {self.bullets_path}")
        return value

    @staticmethod
    def _bullet_score(bullet: dict[str, Any], keywords: list[str]) -> int:
        tags = {str(tag).lower() for tag in bullet.get("keywords", [])}
        score = len(tags.intersection(keywords)) * 4
        text = str(bullet.get("text", ""))
        score += sum(1 for keyword in keywords if _term_present(text, keyword))
        return score

    def _select_bullets(
        self,
        resume_data: dict[str, Any],
        keywords: list[str],
    ) -> dict[str, Any]:
        selected = deepcopy(resume_data)
        for role in selected.get("experience", []):
            maximum = int(role.get("max_bullets", len(role.get("bullets", []))))
            role["bullets"] = sorted(
                role.get("bullets", []),
                key=lambda bullet: self._bullet_score(bullet, keywords),
                reverse=True,
            )[:maximum]
            for subrole in role.get("subroles", []):
                sub_maximum = int(subrole.get("max_bullets", len(subrole.get("bullets", []))))
                subrole["bullets"] = sorted(
                    subrole.get("bullets", []),
                    key=lambda bullet: self._bullet_score(bullet, keywords),
                    reverse=True,
                )[:sub_maximum]
        return selected

    @staticmethod
    def _ordered_skills(profile: Profile, keywords: list[str]) -> list[dict[str, str]]:
        groups: list[dict[str, str]] = []
        for group, skills in profile.skills.items():
            ordered = sorted(
                skills,
                key=lambda skill: (
                    not any(
                        _term_present(skill, keyword) or _term_present(keyword, skill)
                        for keyword in keywords
                    ),
                    skills.index(skill),
                ),
            )
            groups.append({"name": latex_escape(group), "items": ", ".join(map(latex_escape, ordered))})
        return groups

    def _render_context(
        self,
        profile: Profile,
        data: dict[str, Any],
        keywords: list[str],
    ) -> dict[str, Any]:
        experience: list[dict[str, Any]] = []
        for role in data.get("experience", []):
            rendered_role = {
                "company": latex_escape(str(role["company"])),
                "location": latex_escape(str(role["location"])),
                "role": latex_escape(str(role["role"])),
                "dates": latex_escape(str(role["dates"])),
                "bullets": [
                    bold_matching_terms(str(item["text"]), keywords)
                    for item in role.get("bullets", [])
                ],
                "subroles": [],
            }
            for subrole in role.get("subroles", []):
                rendered_role["subroles"].append(
                    {
                        "role": latex_escape(str(subrole["role"])),
                        "dates": latex_escape(str(subrole["dates"])),
                        "bullets": [
                            bold_matching_terms(str(item["text"]), keywords)
                            for item in subrole.get("bullets", [])
                        ],
                    }
                )
            experience.append(rendered_role)

        activities = []
        for activity in data.get("activities", []):
            heading_parts = str(activity["heading"]).split("|", maxsplit=1)
            heading = r"\textbf{" + latex_escape(heading_parts[0].strip()) + "}"
            if len(heading_parts) == 2:
                heading += r" $|$ " + latex_escape(heading_parts[1].strip())
            activities.append(
                {
                    "heading": heading,
                    "dates": latex_escape(str(activity["dates"])),
                    "bullets": [
                        latex_escape(str(bullet)) for bullet in activity.get("bullets", [])
                    ],
                }
            )

        personal = profile.personal
        education = profile.education
        return {
            "personal": {
                "full_name": latex_escape(personal.full_name),
                "phone": latex_escape(personal.phone),
                "email": personal.email,
                "website": personal.website,
                "linkedin": personal.linkedin,
                "github": personal.github,
            },
            "education": {
                "school": latex_escape(education.school),
                "degree": latex_escape(education.degree),
                "majors": latex_escape(" and ".join(education.majors)),
                "minor": latex_escape(education.minor or ""),
                "graduation": latex_escape(
                    f"{education.graduation_month} {education.graduation_year}"
                ),
                "gpa": latex_escape(education.gpa),
            },
            "skill_groups": self._ordered_skills(profile, keywords),
            "experience": experience,
            "activities": activities,
            "certifications": [
                latex_escape(str(value)) for value in data.get("certifications", [])
            ],
        }

    def render_tex(
        self,
        profile: Profile,
        data: dict[str, Any],
        keywords: list[str],
    ) -> str:
        template = self.environment.get_template(self.template_path.name)
        return template.render(**self._render_context(profile, data, keywords))

    @staticmethod
    def _compiler() -> tuple[str, list[str]]:
        if binary := shutil.which("tectonic"):
            return binary, ["--keep-logs", "--synctex", "0"]
        if binary := shutil.which("pdflatex"):
            return binary, ["-interaction=nonstopmode", "-halt-on-error"]
        raise ResumeCompileError(
            "No LaTeX compiler found. Install tectonic and ensure it is available on PATH."
        )

    @classmethod
    def _compile(cls, tex: str, workdir: Path) -> Path:
        source = workdir / "resume.tex"
        source.write_text(tex, encoding="utf-8")
        compiler, args = cls._compiler()
        if Path(compiler).name == "tectonic":
            command = [compiler, *args, "--outdir", str(workdir), str(source)]
        else:
            command = [compiler, *args, "-output-directory", str(workdir), str(source)]
        result = subprocess.run(
            command,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        pdf = workdir / "resume.pdf"
        if result.returncode or not pdf.exists():
            details = (result.stdout + "\n" + result.stderr)[-4000:]
            raise ResumeCompileError(f"LaTeX compilation failed:\n{details}")
        return pdf

    @staticmethod
    def _drop_lowest_optional_bullet(data: dict[str, Any]) -> bool:
        candidates: list[tuple[int, list[dict[str, Any]]]] = []
        for role in data.get("experience", []):
            bullets = role.get("bullets", [])
            minimum = int(role.get("min_bullets", 1))
            if len(bullets) > minimum:
                candidates.append((0, bullets))
            for subrole in role.get("subroles", []):
                sub_bullets = subrole.get("bullets", [])
                sub_minimum = int(subrole.get("min_bullets", 1))
                if len(sub_bullets) > sub_minimum:
                    candidates.append((0, sub_bullets))
        if not candidates:
            return False
        candidates[-1][1].pop()
        return True

    def tailor(
        self,
        job: Job,
        profile: Profile,
        *,
        extra_keywords: list[str] | None = None,
    ) -> TailoredResume:
        if not job.jd_text:
            raise ValueError("Job description must be fetched before tailoring a resume")
        base_data = self._load_resume_data()
        keywords = extract_keywords(job.jd_text, profile, base_data)
        for keyword in extra_keywords or []:
            normalized = keyword.strip().lower()
            if normalized and normalized not in keywords:
                keywords.append(normalized)
        digest_input = (
            "|".join(sorted(keywords))
            + self.template_path.read_text(encoding="utf-8")
            + self.bullets_path.read_text(encoding="utf-8")
        )
        digest = hashlib.sha256(digest_input.encode()).hexdigest()[:12]
        safe_company = re.sub(r"[^a-z0-9]+", "-", job.company.lower()).strip("-")[:40]
        stem = f"{safe_company}-{job.id[:8]}-{digest}"
        output_pdf = self.output_dir / f"{stem}.pdf"
        output_tex = self.output_dir / f"{stem}.tex"
        if output_pdf.exists() and output_tex.exists():
            return TailoredResume(
                output_pdf,
                output_tex,
                keywords,
                len(PdfReader(output_pdf).pages),
                cache_hit=True,
            )

        selected = self._select_bullets(base_data, keywords)
        with tempfile.TemporaryDirectory(prefix="applier-resume-") as temp:
            temp_path = Path(temp)
            while True:
                tex = self.render_tex(profile, selected, keywords)
                pdf = self._compile(tex, temp_path)
                page_count = len(PdfReader(pdf).pages)
                if page_count <= 1 or not self._drop_lowest_optional_bullet(selected):
                    break
            if page_count > 1:
                raise ResumeCompileError(
                    f"Tailored resume is {page_count} pages after dropping optional bullets"
                )
            shutil.copy2(pdf, output_pdf)
            output_tex.write_text(tex, encoding="utf-8")
        return TailoredResume(output_pdf, output_tex, keywords, page_count)
