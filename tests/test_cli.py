from typer.testing import CliRunner

from applier.cli import app


def test_cli_help_lists_workflow_commands():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("sync", "list", "apply", "review", "status", "gmail-sync", "run"):
        assert command in result.stdout
