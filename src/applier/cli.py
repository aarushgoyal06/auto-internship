from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from sqlmodel import Session, select

from applier.config import Profile, Settings, load_profile, load_settings
from applier.db import Application, Job, create_db_engine, init_db
from applier.gmail import sync_gmail_confirmations
from applier.ingest import sync_listings
from applier.runner import ApplicationRunner, RunSummary

app = typer.Typer(
    name="applier",
    no_args_is_help=True,
    help="Local SWE internship application assistant.",
)
console = Console()


def _state() -> tuple[Settings, Profile, object]:
    settings = load_settings()
    profile = load_profile()
    settings.ensure_runtime_dirs()
    engine = create_db_engine(settings.paths.database)
    init_db(engine)
    return settings, profile, engine


def _print_summary(summary: RunSummary) -> None:
    if not summary.counts:
        console.print("No matching applications.")
        return
    table = Table("Outcome", "Count")
    for status, count in sorted(summary.counts.items()):
        table.add_row(status, str(count))
    console.print(table)


@app.command()
def sync() -> None:
    """Download, filter, and store the current Simplify listings."""
    settings, _, engine = _state()
    with console.status("Syncing Simplify listings..."):
        stats = sync_listings(engine, settings.source)
    console.print(
        f"Downloaded {stats.downloaded}; eligible {stats.eligible}; "
        f"inserted {stats.inserted}; updated {stats.updated}; closed {stats.closed}."
    )


@app.command("list")
def list_jobs(
    status: Annotated[str | None, typer.Option(help="Filter by application status.")] = None,
    ats: Annotated[str | None, typer.Option(help="Filter by ATS type.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 50,
) -> None:
    """List tracked applications."""
    _, _, engine = _state()
    with Session(engine) as session:
        statement = (
            select(Application, Job)
            .join(Job, Application.job_id == Job.id)
            .order_by(Job.date_posted.desc())
            .limit(limit)
        )
        if status:
            statement = statement.where(Application.status == status)
        if ats:
            statement = statement.where(Job.ats_type == ats)
        rows = session.exec(statement).all()
    table = Table("Status", "ATS", "Company", "Role", "Location", "Job ID")
    for application, job in rows:
        table.add_row(
            application.status,
            job.ats_type,
            job.company,
            job.title,
            "; ".join(job.locations),
            job.id,
        )
    console.print(table)
    console.print(f"{len(rows)} row(s)")


@app.command("apply")
def apply_jobs(
    limit: Annotated[int | None, typer.Option(min=1, help="Maximum jobs this run.")] = None,
    ats: Annotated[str | None, typer.Option(help="Only apply on this ATS.")] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Fill and capture forms without submitting."),
    ] = False,
) -> None:
    """Tailor resumes and process queued applications."""
    settings, profile, engine = _state()
    runner = ApplicationRunner(engine, settings, profile)
    if runner.llm_warning:
        console.print(f"[yellow]LLM disabled:[/yellow] {runner.llm_warning}")
    with console.status("Processing applications..."):
        summary = runner.apply(limit=limit, ats_type=ats, dry_run=dry_run)
    _print_summary(summary)


@app.command()
def review(
    limit: Annotated[int | None, typer.Option(min=1)] = None,
) -> None:
    """Open needs-review applications in a visible browser."""
    settings, profile, engine = _state()
    runner = ApplicationRunner(engine, settings, profile)

    def confirm(job: Job, unresolved: list[str]) -> bool:
        console.print(f"\n[bold]{job.company} — {job.title}[/bold]")
        console.print(job.url)
        if unresolved:
            console.print("[yellow]Still unresolved:[/yellow] " + ", ".join(unresolved))
        console.print(
            "Complete the form (including any CAPTCHA) in the browser, then return here."
        )
        return typer.confirm("Was the application successfully submitted?", default=False)

    summary = runner.review(confirm, limit=limit)
    _print_summary(summary)


@app.command()
def status() -> None:
    """Show status totals and recent failures."""
    _, _, engine = _state()
    with Session(engine) as session:
        applications = list(session.exec(select(Application)).all())
        failures = list(
            session.exec(
                select(Application, Job)
                .join(Job, Application.job_id == Job.id)
                .where(Application.status.in_(["failed", "needs_review"]))
                .order_by(Application.updated_at.desc())
                .limit(10)
            ).all()
        )
    counts = Counter(application.status for application in applications)
    table = Table("Status", "Count")
    for name in ("queued", "applied", "confirmed", "needs_review", "failed", "skipped"):
        table.add_row(name, str(counts.get(name, 0)))
    console.print(table)
    if failures:
        recent = Table("Status", "Company", "Role", "Reason")
        for application, job in failures:
            recent.add_row(
                application.status,
                job.company,
                job.title,
                (application.error or "")[:120],
            )
        console.print("\nRecent failures/review items")
        console.print(recent)


@app.command("gmail-sync")
def gmail_sync() -> None:
    """Use read-only Gmail OAuth to find application confirmations."""
    settings, _, engine = _state()
    stats = sync_gmail_confirmations(
        engine,
        credentials_path=settings.paths.gmail_credentials,
        token_path=settings.paths.gmail_token,
    )
    console.print(f"Scanned {stats.scanned} message(s); matched {stats.matched}.")


@app.command()
def tailor(job_id: str) -> None:
    """Fetch one job description and compile its tailored resume."""
    settings, profile, engine = _state()
    runner = ApplicationRunner(engine, settings, profile)
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if job is None:
            raise typer.BadParameter(f"Unknown job id: {job_id}")
        application = session.exec(
            select(Application).where(Application.job_id == job_id)
        ).first()
        if application is None:
            raise typer.BadParameter(f"No application record for job id: {job_id}")
        path = runner._prepare_resume(session, job, application)
    console.print(f"Created {path}")


@app.command()
def run(
    limit: Annotated[int | None, typer.Option(min=1)] = None,
) -> None:
    """Sync listings, apply to the queue, then check Gmail."""
    settings, profile, engine = _state()
    stats = sync_listings(engine, settings.source)
    console.print(f"Synced {stats.eligible} eligible listing(s).")
    runner = ApplicationRunner(engine, settings, profile)
    _print_summary(runner.apply(limit=limit))
    if settings.paths.gmail_credentials.exists() or settings.paths.gmail_token.exists():
        gmail_stats = sync_gmail_confirmations(
            engine,
            credentials_path=settings.paths.gmail_credentials,
            token_path=settings.paths.gmail_token,
        )
        console.print(
            f"Gmail: scanned {gmail_stats.scanned}; matched {gmail_stats.matched}."
        )
    else:
        console.print("[yellow]Gmail sync skipped: credentials.json is not configured.[/yellow]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
