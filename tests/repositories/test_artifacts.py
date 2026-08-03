from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

from flyboard.models import (
    ArtifactIndexEntry,
    ProjectDataListArtifact,
    ProjectRecord,
    RunSummary,
    build_current_field_table,
)
from flyboard.repositories import ArtifactRepository, ProjectRepository
from flyboard.services import ServiceError

NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


def repositories(tmp_path):
    projects = ProjectRepository(tmp_path)
    with projects.mutate("prj_test") as mutation:
        mutation.create(
            ProjectRecord(
                project_id="prj_test",
                project_name="测试",
                created_at=NOW,
                updated_at=NOW,
                materials=[],
                latest_run_id=None,
            )
        )
    return projects, ArtifactRepository(tmp_path)


def data_entry() -> ArtifactIndexEntry:
    return ArtifactIndexEntry(
        path="project_data_list.json",
        kind="project_data_list",
        producer_phase="rule_check",
        consumers=["api"],
        access_level="project_scoped",
        source_material_ids=[],
    )


def test_artifact_repository_writes_and_reads_typed_artifact(tmp_path) -> None:
    _, artifacts = repositories(tmp_path)
    artifact = ProjectDataListArtifact(schema_version="1.0", run_id="run_1", rows=[])
    assert (
        artifacts.replace_json_artifact("prj_test", "run_1", artifact=artifact, entry=data_entry())
        == "project_data_list.json"
    )
    assert artifacts.read_indexed_project_data_list("prj_test", "run_1", data_entry()) == []


def test_artifact_reader_distinguishes_version_failures(tmp_path) -> None:
    _, artifacts = repositories(tmp_path)
    path = tmp_path / "projects" / "prj_test" / "runs" / "run_1" / "project_data_list.json"
    path.parent.mkdir(parents=True)
    cases = [
        ('{"run_id":"run_1","rows":[]}', "artifact_schema_version_missing"),
        (
            '{"schema_version":"2.0","run_id":"run_1","rows":[]}',
            "artifact_schema_version_unsupported",
        ),
        (
            '{"schema_version":"1.0","run_id":"run_1","rows":[],"extra":1}',
            "artifact_schema_invalid",
        ),
    ]
    for content, expected in cases:
        path.write_text(content, encoding="utf-8")
        with pytest.raises(ServiceError) as error:
            artifacts.read_fallback_project_data_list("prj_test", "run_1", "project_data_list.json")
        assert error.value.code == expected


def test_project_scoped_content_requires_exact_indexed_path(tmp_path) -> None:
    _, artifacts = repositories(tmp_path)
    entry = ArtifactIndexEntry(
        path="preview/layer.svg",
        kind="gerber_preview",
        producer_phase="gerber_preparation",
        consumers=["gui"],
        access_level="project_scoped",
        source_material_ids=["mat_1"],
    )
    artifacts.replace_content_artifact(
        "prj_test",
        "run_1",
        content=b"<svg/>",
        media_type="image/svg+xml",
        entry=entry,
    )
    content, media_type = artifacts.read_project_scoped_content(
        "prj_test", "run_1", entry=entry, expected_path=entry.path
    )
    assert content == b"<svg/>"
    assert media_type == "image/svg+xml"
    with pytest.raises(ServiceError):
        artifacts.read_project_scoped_content(
            "prj_test", "run_1", entry=entry, expected_path="other.svg"
        )


class ModuleSidecar(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1.0"]
    run_id: str
    payload: str


def test_json_writer_accepts_strict_run_scoped_module_artifact(tmp_path) -> None:
    _, artifacts = repositories(tmp_path)
    artifact = ModuleSidecar(schema_version="1.0", run_id="run_1", payload="canonical")

    assert (
        artifacts.replace_json_artifact("prj_test", "run_1", artifact=artifact, entry=data_entry())
        == "project_data_list.json"
    )
    assert (
        tmp_path / "projects" / "prj_test" / "runs" / "run_1" / "project_data_list.json"
    ).read_text(encoding="utf-8") == (
        '{"payload":"canonical","run_id":"run_1","schema_version":"1.0"}\n'
    )


def test_json_writer_rejects_models_without_run_identity(tmp_path) -> None:
    _, artifacts = repositories(tmp_path)
    with pytest.raises(ServiceError) as error:
        artifacts.replace_json_artifact(
            "prj_test",
            "run_1",
            artifact=build_current_field_table(),
            entry=data_entry(),
        )
    assert error.value.code == "artifact_schema_invalid"


def test_run_io_lock_serializes_summary_reader_and_atomic_replace(tmp_path, monkeypatch) -> None:
    import flyboard.repositories.artifacts as artifacts_module

    _, artifacts = repositories(tmp_path)
    summary = RunSummary.model_validate(
        {
            "schema_version": "1.0",
            "run_id": "run_1",
            "project_id": "prj_test",
            "status": "queued",
            "current_phase": None,
            "phase_timings": [],
            "branch_status": {
                "pdf_image": "queued",
                "xlsx": "queued",
                "gerber": "queued",
            },
            "rule_check_summary": {
                "status": "skipped",
                "rule_set_id": None,
                "rule_set_version": None,
                "attempted_rule_count": 0,
                "error_count": 0,
                "reason": "rule_set_not_selected",
            },
            "model_call_summaries": [],
            "error_summary": [],
            "stop_requested_at": None,
            "stop_reason": None,
            "started_at": NOW,
            "finished_at": None,
            "duration_ms": None,
            "artifact_index_path": "artifact_index.json",
            "project_data_list_path": None,
        }
    )
    artifacts.replace_run_summary("prj_test", "run_1", summary)
    read_started = threading.Event()
    allow_read = threading.Event()
    replace_entered = threading.Event()
    errors: list[Exception] = []
    original_read_bytes = Path.read_bytes
    original_replace = artifacts_module.atomic_replace_bytes

    def blocked_read(path):
        if path.name == "run_summary.json" and threading.current_thread().name == "reader":
            read_started.set()
            allow_read.wait(1)
        return original_read_bytes(path)

    def observed_replace(path, content):
        if path.name == "run_summary.json":
            replace_entered.set()
        return original_replace(path, content)

    monkeypatch.setattr(Path, "read_bytes", blocked_read)
    monkeypatch.setattr(artifacts_module, "atomic_replace_bytes", observed_replace)

    def read_summary():
        try:
            artifacts.read_run_summary("prj_test", "run_1")
        except Exception as exc:
            errors.append(exc)

    def write_summary():
        try:
            artifacts.replace_run_summary("prj_test", "run_1", summary)
        except Exception as exc:
            errors.append(exc)

    reader = threading.Thread(target=read_summary, name="reader")
    writer = threading.Thread(target=write_summary, name="writer")
    reader.start()
    assert read_started.wait(1)
    writer.start()
    assert not replace_entered.wait(0.1)
    allow_read.set()
    reader.join(1)
    writer.join(1)

    assert replace_entered.is_set()
    assert not reader.is_alive()
    assert not writer.is_alive()
    assert errors == []
