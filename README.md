# SWE Internship Auto-Applier

A local, auditable application assistant for the
[SimplifyJobs Summer 2027 internship list](https://github.com/SimplifyJobs/Summer2027-Internships).
It keeps open US software-engineering roles, creates a truthful job-specific resume from a
LaTeX source, and can submit applications on Greenhouse, Lever, and Ashby. Workday, unknown
forms, CAPTCHAs, and fields without a safe answer go to a manual review queue.

The tool never attempts to solve or evade a CAPTCHA. It also never adds an unverified skill or
experience to a resume or generated answer.

## What it does

- Reads Simplify's structured `listings.json` feed rather than scraping the README.
- Stores jobs, attempts, artifacts, and statuses in local SQLite.
- Filters to active, visible Summer 2027 SWE roles with at least one US location.
- Fetches job descriptions through Greenhouse, Lever, and Ashby public endpoints, with an HTML
  fallback.
- Reorders known skills and real resume bullets by relevance, then compiles a one-page PDF.
- Uses profile rules before an LLM. Gemini's free tier is the default; Groq and Ollama are
  supported.
- Auto-submits supported forms, saves screenshots and Playwright traces, and queues anything
  uncertain for review.
- Uses read-only Gmail OAuth to detect application confirmation emails.

## Install

Requirements:

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- [`tectonic`](https://tectonic-typesetting.github.io/) (preferred) or `pdflatex`

```bash
uv sync
uv run playwright install chromium
cp .env.example .env
```

Add a free `GEMINI_API_KEY` to `.env`. You may instead set `llm.provider: ollama` in
`config/settings.yaml` and run Ollama locally. A Groq key is an optional fallback.

Review `config/profile.yaml` before applying. The street address, city, state, postal code, and
availability are deliberately blank because they were not supplied. Any form that requires one
of them is sent to manual review rather than filled with guessed data.

## Quick start

```bash
# Download and filter the current list
uv run applier sync

# Inspect queued roles
uv run applier list --status queued

# Compile and fill one application without submitting it
uv run applier apply --dry-run --limit 1

# Auto-submit supported queued applications
uv run applier apply

# Open unresolved applications in a visible browser
uv run applier review

# Show status totals and recent failures
uv run applier status
```

`apply` uses 20–60 second pauses and one browser session to reduce accidental bursts. Override
the range in `config/settings.yaml`; `--limit` is recommended for the first live runs.

## Configuration

`config/profile.yaml` is the sole source of applicant facts. It contains contact details,
education, authorization, relocation and EEO preferences, and the skills that may appear in
tailored resumes.

`config/settings.yaml` controls:

- source categories, term, and country filter
- browser mode, retry count, pacing, and navigation timeout
- LLM provider and models
- SQLite, resume, screenshot, trace, and Gmail token locations

Secrets belong in `.env` and are ignored by Git. OAuth credentials and all files under `data/`
are also ignored.

## Resume tailoring

The LaTeX/Jinja template is `resume/base.tex.j2`; factual bullets and their matching tags are in
`resume/bullets.yaml`. Tailoring may:

1. rank skills already listed in the profile,
2. reorder and select existing bullets,
3. bold matching truthful keywords, and
4. remove the lowest-ranked optional bullet if the PDF exceeds one page.

It may not create new resume claims. Generated `.tex` and `.pdf` files are kept together under
`data/resumes/` for inspection.

To test compilation without opening a browser:

```bash
uv run applier tailor JOB_ID
```

## Application behavior

Statuses are `queued`, `applied`, `failed`, `needs_review`, `skipped`, and `confirmed`.

- Greenhouse, Lever, and Ashby use Playwright adapters.
- Workday, iCIMS, SmartRecruiters, and custom sites start in `needs_review`.
- Unknown required questions, required cover letters, missing profile fields, validation
  failures, and visible CAPTCHAs also become `needs_review`.
- Required privacy/accuracy acknowledgements are checked; optional checkboxes are left alone.
- Demographic questions select a visible “prefer not to say” equivalent.
- Every generated answer is saved with its source (`profile`, `fixed`, `eeo_default`, or `llm`).

Run a dry-run first. A dry-run never clicks Submit. A clean fill stays queued; a CAPTCHA,
missing form, or unresolved required field is moved to `needs_review`, and preparation/browser
errors are recorded as `failed`.

## Gmail confirmations

1. Create a Google Cloud desktop OAuth client and enable the Gmail API.
2. Download its OAuth file to `credentials.json`.
3. Run:

```bash
uv run applier gmail-sync
```

The first run opens Google's consent flow for the read-only
`https://www.googleapis.com/auth/gmail.readonly` scope. The refresh token is stored in
`data/gmail_token.json`. Messages from the last 14 days with application-related subjects are
matched to submitted jobs by company, role, sender domain, and submission time. Gmail access
never sends, deletes, or labels mail.

## Scheduling

`run` performs `sync`, `apply`, and `gmail-sync` in order:

```bash
uv run applier run
```

Example cron entry (every two hours):

```cron
0 */2 * * * cd /absolute/path/to/project && /absolute/path/to/uv run applier run >> data/cron.log 2>&1
```

Use an absolute project path and obtain the `uv` path with `command -v uv`. Gmail sync is skipped
with a clear warning until OAuth credentials are configured.

## Development

```bash
uv run ruff check .
uv run mypy src
uv run pytest
```

Tests use saved local HTML fixtures and mocked network responses. They never submit a real
application.
