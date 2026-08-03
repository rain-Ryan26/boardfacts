from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from flyboard.config import AppConfig, load_local_env

BASE = "FLYBOARD_QWEN_API_BASE"
KEY = "FLYBOARD_QWEN_API_KEY"
STORAGE_ROOT = "FLYBOARD_STORAGE_ROOT"


def test_load_local_env_reads_dotenv_without_overwriting_process_env(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(BASE, raising=False)
    monkeypatch.setenv(KEY, "process-secret")
    env_file = tmp_path / ".env"
    env_file.write_text(
        f'{BASE}="https://example.test/v1"\n{KEY}="file-secret"\n',
        encoding="utf-8",
    )

    try:
        assert load_local_env(env_file)
        assert os.environ[BASE] == "https://example.test/v1"
        assert os.environ[KEY] == "process-secret"
    finally:
        os.environ.pop(BASE, None)


def test_cli_loads_local_env_before_running_command(monkeypatch) -> None:
    cli_main = importlib.import_module("flyboard.cli.main")
    observed: list[str] = []
    monkeypatch.setattr(cli_main, "load_local_env", lambda: observed.append("env"))
    monkeypatch.setattr(cli_main, "_serve", lambda args: observed.append("action") or 0)

    assert cli_main.main(["serve"]) == 0
    assert observed == ["env", "action"]


def test_app_config_requires_absolute_storage_root(tmp_path) -> None:
    with pytest.raises(ValueError, match="absolute path"):
        AppConfig(Path("relative-storage"))

    configured = AppConfig(tmp_path / "storage")
    assert configured.storage_root == (tmp_path / "storage").resolve()


def test_app_config_from_env_requires_non_empty_absolute_root(tmp_path) -> None:
    with pytest.raises(ValueError, match="is required"):
        AppConfig.from_env({})
    with pytest.raises(ValueError, match="must not be empty"):
        AppConfig.from_env({STORAGE_ROOT: "  "})
    with pytest.raises(ValueError, match="absolute path"):
        AppConfig.from_env({STORAGE_ROOT: "relative-storage"})

    configured = AppConfig.from_env({STORAGE_ROOT: str(tmp_path / "env-storage")})
    assert configured.storage_root == (tmp_path / "env-storage").resolve()


def test_serve_prefers_explicit_storage_root(tmp_path, monkeypatch) -> None:
    cli_main = importlib.import_module("flyboard.cli.main")
    explicit_root = tmp_path / "explicit-storage"
    monkeypatch.setenv(STORAGE_ROOT, str(tmp_path / "env-storage"))
    observed = []

    class FakeApplication:
        def close(self) -> None:
            observed.append("closed")

    def fake_build_application(config):
        observed.append(config.storage_root)
        return FakeApplication()

    monkeypatch.setattr(cli_main, "build_application", fake_build_application)
    monkeypatch.setattr(cli_main.uvicorn, "run", lambda *args, **kwargs: observed.append("run"))
    args = cli_main.build_parser().parse_args(["serve", "--storage-root", str(explicit_root)])

    assert cli_main._serve(args) == 0
    assert observed == [explicit_root.resolve(), "run", "closed"]


def test_serve_uses_environment_when_cli_root_is_absent(tmp_path, monkeypatch) -> None:
    cli_main = importlib.import_module("flyboard.cli.main")
    env_root = tmp_path / "env-storage"
    monkeypatch.setenv(STORAGE_ROOT, str(env_root))
    observed = []

    class FakeApplication:
        def close(self) -> None:
            observed.append("closed")

    monkeypatch.setattr(
        cli_main,
        "build_application",
        lambda config: observed.append(config.storage_root) or FakeApplication(),
    )
    monkeypatch.setattr(cli_main.uvicorn, "run", lambda *args, **kwargs: observed.append("run"))
    args = cli_main.build_parser().parse_args(["serve"])

    assert cli_main._serve(args) == 0
    assert observed == [env_root.resolve(), "run", "closed"]


def test_serve_reports_missing_storage_root(monkeypatch, capsys) -> None:
    cli_main = importlib.import_module("flyboard.cli.main")
    monkeypatch.delenv(STORAGE_ROOT, raising=False)
    args = cli_main.build_parser().parse_args(["serve"])

    assert cli_main._serve(args) == 2
    output = capsys.readouterr().out
    assert '"code": "configuration_invalid"' in output
    assert "FLYBOARD_STORAGE_ROOT is required" in output
