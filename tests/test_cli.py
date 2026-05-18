from click.testing import CliRunner

from sentry_taskbroker_management.cli import main


def test_cli_help() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "sentry-taskbroker-management" in result.output


def test_cli_version() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "sentry-taskbroker-management" in result.output
