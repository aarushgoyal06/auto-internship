from pathlib import Path

from pypdf import PdfWriter

from applier.db import Job
from applier.tailor import ResumeTailor, extract_keywords


def _one_page_pdf(path: Path) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def test_keyword_extraction_only_uses_known_facts(profile):
    tailor = ResumeTailor()
    data = tailor._load_resume_data()
    keywords = extract_keywords(
        "We need Python, AWS Lambda, PostgreSQL, Terraform, and Rust.",
        profile,
        data,
    )
    assert {"python", "aws", "lambda", "postgresql", "terraform"}.issubset(keywords)
    assert "rust" not in keywords


def test_tailor_renders_truthful_one_page_and_caches(profile, tmp_path, monkeypatch):
    output_dir = tmp_path / "resumes"
    tailor = ResumeTailor(output_dir=output_dir)
    job = Job(
        id="12345678-abcd",
        company="Acme & Sons",
        title="Software Engineer Intern",
        url="https://example.test/job",
        ats_type="other",
        jd_text="Build Python services on AWS Lambda with Terraform and PostgreSQL.",
    )

    def fake_compile(tex: str, workdir: Path) -> Path:
        assert "Rust" not in tex
        assert r"\textbf{Python}" in tex
        return _one_page_pdf(workdir / "resume.pdf")

    monkeypatch.setattr(ResumeTailor, "_compile", staticmethod(fake_compile))
    first = tailor.tailor(job, profile)
    second = tailor.tailor(job, profile)

    assert first.page_count == 1
    assert first.pdf_path.exists()
    assert first.tex_path.exists()
    assert not first.cache_hit
    assert second.cache_hit
    assert second.pdf_path == first.pdf_path
    source = first.tex_path.read_text(encoding="utf-8")
    assert "Aarush Goyal" in source
    assert r"Acme \& Sons" not in source
    assert "reducing manual processing time by 35" in source
