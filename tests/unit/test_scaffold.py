import json

import pytest
from typer.testing import CliRunner

from opspilot import __version__
from opspilot.cli import app
from opspilot.config import Settings
from opspilot.logging import configure_logging, get_logger

runner = CliRunner()


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPSPILOT_LLM_MODEL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.llm_model == "qwen3:4b"
    assert settings.admin_context == "k3d-opspilot"
    assert settings.reader_kubeconfig.name == "opspilot-reader.kubeconfig"
    assert settings.operator_kubeconfig.name == "opspilot-operator.kubeconfig"


def test_settings_env_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSPILOT_LLM_MODEL", "other:1b")
    monkeypatch.setenv("OPSPILOT_LOG_LEVEL", "DEBUG")
    settings = Settings(_env_file=None)
    assert settings.llm_model == "other:1b"
    assert settings.log_level == "DEBUG"


def test_logging_emits_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", json=True)
    get_logger("test").info("hello", answer=42)
    line = capsys.readouterr().err.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["event"] == "hello"
    assert record["answer"] == 42
    assert record["logger"] == "test"
    assert record["level"] == "info"


def test_logging_filters_below_level(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING", json=True)
    get_logger("test").info("hidden")
    assert "hidden" not in capsys.readouterr().err


def test_cli_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_cli_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "info" in result.output


def test_cli_info() -> None:
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0
    assert "llm_model" in result.output
