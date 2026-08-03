from __future__ import annotations

import json
import mimetypes
import os
import threading
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from flyboard.errors import ServiceError
from flyboard.models import (
    AccessLevel,
    ArtifactIndex,
    ArtifactIndexEntry,
    EvidenceIndex,
    GerberJobPreparation,
    LayerDefinition,
    LayerDefinitionsArtifact,
    ProjectDataListArtifact,
    ProjectDataRow,
    RuleSetSnapshot,
    RunInputSnapshot,
    RunSummary,
    ToolFinding,
    ToolFindingsArtifact,
    TrajectoryEvent,
    unwrap_project_data_list,
)
from flyboard.repositories._filesystem import (
    atomic_replace_bytes,
    ensure_no_reparse_points,
    json_bytes,
    managed_child,
    validate_identifier,
)

ModelT = TypeVar("ModelT", bound=BaseModel)


def _media_type(path: str) -> str:
    suffix = Path(path).suffix.lower()
    overrides = {".svg": "image/svg+xml", ".json": "application/json"}
    return overrides.get(suffix) or mimetypes.guess_type(path)[0] or "application/octet-stream"


class ArtifactRepository:
    """Typed, run-root-confined artifact I/O without business-state updates."""

    def __init__(self, storage_root: Path) -> None:
        self.storage_root = storage_root
        self.projects_root = storage_root / "projects"
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.projects_root.mkdir(parents=True, exist_ok=True)
        ensure_no_reparse_points(self.storage_root, self.projects_root)
        self._append_guard = threading.Lock()
        self._append_locks: dict[tuple[str, str], threading.Lock] = {}
        self._io_guard = threading.Lock()
        self._io_locks: dict[tuple[str, str], threading.RLock] = {}

    def _io_lock(self, project_id: str, run_id: str) -> threading.RLock:
        key = (project_id, run_id)
        with self._io_guard:
            return self._io_locks.setdefault(key, threading.RLock())

    def _run_root(self, project_id: str, run_id: str, *, create: bool = False) -> Path:
        project_id = validate_identifier(project_id)
        run_id = validate_identifier(run_id)
        project_root = managed_child(self.projects_root, project_id)
        if not project_root.is_dir():
            raise ServiceError("project_not_found")
        run_root = project_root / "runs" / run_id
        ensure_no_reparse_points(self.storage_root, run_root)
        if create:
            run_root.mkdir(parents=True, exist_ok=True)
            ensure_no_reparse_points(self.storage_root, run_root)
        return run_root

    def _artifact_path(
        self, project_id: str, run_id: str, locator: str, *, create_run: bool = False
    ) -> Path:
        root = self._run_root(project_id, run_id, create=create_run)
        path = managed_child(root, locator, locator=True)
        ensure_no_reparse_points(root, path)
        return path

    def replace_json_artifact(
        self,
        project_id: str,
        run_id: str,
        *,
        artifact: BaseModel,
        entry: ArtifactIndexEntry,
    ) -> str:
        model_fields = type(artifact).model_fields if isinstance(artifact, BaseModel) else {}
        if (
            not isinstance(artifact, BaseModel)
            or "schema_version" not in model_fields
            or ("run_id" not in model_fields and not isinstance(artifact, RuleSetSnapshot))
        ):
            raise ServiceError("artifact_schema_invalid")
        try:
            canonical = type(artifact).model_validate(artifact.model_dump(mode="json"))
        except ValidationError as exc:
            raise ServiceError("artifact_schema_invalid") from exc
        if canonical.schema_version != "1.0":
            raise ServiceError("artifact_schema_invalid")
        artifact_run_id = getattr(canonical, "run_id", run_id)
        if artifact_run_id != run_id:
            raise ServiceError("artifact_identity_mismatch")
        try:
            with self._io_lock(project_id, run_id):
                atomic_replace_bytes(
                    self._artifact_path(project_id, run_id, entry.path, create_run=True),
                    json_bytes(canonical),
                )
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError("artifact_write_failed") from exc
        return entry.path

    def replace_content_artifact(
        self,
        project_id: str,
        run_id: str,
        *,
        content: bytes,
        media_type: str,
        entry: ArtifactIndexEntry,
    ) -> str:
        if not media_type or media_type != _media_type(entry.path):
            raise ServiceError("artifact_media_type_invalid")
        try:
            with self._io_lock(project_id, run_id):
                atomic_replace_bytes(
                    self._artifact_path(project_id, run_id, entry.path, create_run=True),
                    content,
                )
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError("artifact_write_failed") from exc
        return entry.path

    def _replace_report(self, project_id: str, run_id: str, markdown: str) -> None:
        if not isinstance(markdown, str):
            raise ServiceError("report_render_failed")
        try:
            with self._io_lock(project_id, run_id):
                run_root = self._run_root(project_id, run_id)
                if not run_root.is_dir():
                    raise ServiceError("run_not_found")
                atomic_replace_bytes(run_root / "report.md", markdown.encode("utf-8"))
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError("report_write_failed") from exc

    def replace_run_summary(self, project_id: str, run_id: str, summary: RunSummary) -> None:
        if summary.run_id != run_id or summary.project_id != project_id:
            raise ServiceError("artifact_identity_mismatch")
        try:
            with self._io_lock(project_id, run_id):
                atomic_replace_bytes(
                    self._artifact_path(project_id, run_id, "run_summary.json", create_run=True),
                    json_bytes(summary),
                )
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError("run_record_write_failed") from exc

    def replace_artifact_index(self, project_id: str, run_id: str, index: ArtifactIndex) -> None:
        if index.run_id != run_id:
            raise ServiceError("artifact_identity_mismatch")
        try:
            with self._io_lock(project_id, run_id):
                atomic_replace_bytes(
                    self._artifact_path(project_id, run_id, "artifact_index.json", create_run=True),
                    json_bytes(index),
                )
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError("artifact_index_update_failed") from exc

    def append_trajectory(self, project_id: str, run_id: str, event: TrajectoryEvent) -> None:
        if event.run_id != run_id:
            raise ServiceError("artifact_identity_mismatch")
        key = (project_id, run_id)
        with self._append_guard:
            lock = self._append_locks.setdefault(key, threading.Lock())
        path = self._artifact_path(project_id, run_id, "trajectory.jsonl", create_run=True)
        line = json_bytes(event)
        with self._io_lock(project_id, run_id), lock:
            try:
                ensure_no_reparse_points(self.storage_root, path)
                descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "ab") as stream:
                    stream.write(line)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as exc:
                raise ServiceError("run_record_write_failed") from exc

    def _read_versioned(
        self,
        project_id: str,
        run_id: str,
        locator: str,
        model: type[ModelT],
        *,
        unavailable_code: str,
    ) -> ModelT:
        try:
            with self._io_lock(project_id, run_id):
                path = self._artifact_path(project_id, run_id, locator)
                raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise ServiceError(unavailable_code) from exc
        except OSError as exc:
            raise ServiceError(unavailable_code) from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ServiceError("artifact_schema_invalid") from exc
        if type(data) is not dict:
            raise ServiceError("artifact_schema_invalid")
        if "schema_version" not in data:
            raise ServiceError("artifact_schema_version_missing")
        if data["schema_version"] != "1.0":
            raise ServiceError("artifact_schema_version_unsupported")
        if model is RunInputSnapshot and isinstance(data.get("field_table"), dict):
            field_table = data["field_table"]
            if "schema_version" not in field_table:
                raise ServiceError("artifact_schema_version_missing")
            if field_table["schema_version"] != "1.0":
                raise ServiceError("artifact_schema_version_unsupported")
        try:
            result = model.model_validate(data)
        except ValidationError as exc:
            raise ServiceError("artifact_schema_invalid") from exc
        persisted_run_id = getattr(result, "run_id", run_id)
        if persisted_run_id != run_id:
            raise ServiceError("artifact_schema_invalid")
        return result

    def read_run_summary(self, project_id: str, run_id: str) -> RunSummary:
        summary = self._read_versioned(
            project_id,
            run_id,
            "run_summary.json",
            RunSummary,
            unavailable_code="run_not_found",
        )
        if summary.project_id != project_id:
            raise ServiceError("artifact_schema_invalid")
        return summary.model_copy(deep=True)

    def list_run_summaries(self, project_id: str) -> list[RunSummary]:
        project_id = validate_identifier(project_id)
        project_root = managed_child(self.projects_root, project_id)
        runs_root = project_root / "runs"
        ensure_no_reparse_points(self.storage_root, runs_root)
        if not runs_root.exists():
            return []
        try:
            run_ids = sorted(item.name for item in runs_root.iterdir() if item.is_dir())
        except OSError as exc:
            raise ServiceError("run_summary_scan_failed") from exc
        summaries: list[RunSummary] = []
        for run_id in run_ids:
            try:
                validate_identifier(run_id)
            except ServiceError:
                continue
            summary_path = self._artifact_path(project_id, run_id, "run_summary.json")
            if not summary_path.exists():
                continue
            summaries.append(self.read_run_summary(project_id, run_id))
        return summaries

    def read_artifact_index(self, project_id: str, run_id: str, index_path: str) -> ArtifactIndex:
        return self._read_versioned(
            project_id,
            run_id,
            index_path,
            ArtifactIndex,
            unavailable_code="artifact_index_unavailable",
        ).model_copy(deep=True)

    def _read_project_data_list(
        self, project_id: str, run_id: str, locator: str
    ) -> list[ProjectDataRow]:
        artifact = self._read_versioned(
            project_id,
            run_id,
            locator,
            ProjectDataListArtifact,
            unavailable_code="project_data_list_unavailable",
        )
        return unwrap_project_data_list(artifact)

    def read_indexed_project_data_list(
        self, project_id: str, run_id: str, entry: ArtifactIndexEntry
    ) -> list[ProjectDataRow]:
        if entry.kind != "project_data_list":
            raise ServiceError("project_data_list_unavailable")
        return self._read_project_data_list(project_id, run_id, entry.path)

    def read_fallback_project_data_list(
        self, project_id: str, run_id: str, summary_path: str
    ) -> list[ProjectDataRow]:
        return self._read_project_data_list(project_id, run_id, summary_path)

    def read_tool_findings(
        self, project_id: str, run_id: str, entry: ArtifactIndexEntry
    ) -> list[ToolFinding]:
        if entry.kind != "tool_findings":
            raise ServiceError("tool_findings_unavailable")
        artifact = self._read_versioned(
            project_id,
            run_id,
            entry.path,
            ToolFindingsArtifact,
            unavailable_code="tool_findings_unavailable",
        )
        return [item.model_copy(deep=True) for item in artifact.tool_findings]

    def read_evidence_index(
        self, project_id: str, run_id: str, entry: ArtifactIndexEntry
    ) -> EvidenceIndex:
        if entry.kind != "evidence_index":
            raise ServiceError("evidence_index_unavailable")
        return self._read_versioned(
            project_id,
            run_id,
            entry.path,
            EvidenceIndex,
            unavailable_code="evidence_index_unavailable",
        ).model_copy(deep=True)

    def read_project_scoped_content(
        self,
        project_id: str,
        run_id: str,
        *,
        entry: ArtifactIndexEntry,
        expected_path: str,
    ) -> tuple[bytes, str]:
        if entry.access_level != AccessLevel.PROJECT_SCOPED or entry.path != expected_path:
            raise ServiceError("artifact_content_unavailable")
        try:
            with self._io_lock(project_id, run_id):
                content = self._artifact_path(project_id, run_id, entry.path).read_bytes()
        except OSError as exc:
            raise ServiceError("artifact_content_unavailable") from exc
        return content, _media_type(entry.path)

    def _read_indexed_input_snapshot(
        self, project_id: str, run_id: str, artifact_index: ArtifactIndex
    ) -> RunInputSnapshot:
        entries = [
            entry
            for entry in artifact_index.entries
            if entry.kind == "run_input_snapshot" and entry.path == "run_input_snapshot.json"
        ]
        if len(entries) != 1:
            raise ServiceError("gerber_preparations_unavailable")
        snapshot = self._read_versioned(
            project_id,
            run_id,
            entries[0].path,
            RunInputSnapshot,
            unavailable_code="gerber_preparations_unavailable",
        )
        if snapshot.project_id != project_id:
            raise ServiceError("artifact_schema_invalid")
        return snapshot

    def read_gerber_preparations(
        self, project_id: str, run_id: str, artifact_index: ArtifactIndex
    ) -> list[GerberJobPreparation]:
        if artifact_index.run_id != run_id:
            raise ServiceError("artifact_schema_invalid")
        snapshot = self._read_indexed_input_snapshot(project_id, run_id, artifact_index)
        entries = [entry for entry in artifact_index.entries if entry.kind == "gerber_preparation"]
        by_job: dict[str, GerberJobPreparation] = {}
        for entry in entries:
            result = self._read_versioned(
                project_id,
                run_id,
                entry.path,
                GerberJobPreparation,
                unavailable_code="gerber_preparations_unavailable",
            )
            if (
                entry.source_material_ids != [result.job_material_id]
                or result.project_id != project_id
                or result.job_material_id not in snapshot.groups.gerber_jobs
                or result.job_material_id in by_job
            ):
                raise ServiceError("artifact_schema_invalid")
            by_job[result.job_material_id] = result
        return [
            by_job[job_id].model_copy(deep=True)
            for job_id in snapshot.groups.gerber_jobs
            if job_id in by_job
        ]

    def read_gerber_layer_preview(
        self,
        project_id: str,
        run_id: str,
        artifact_index: ArtifactIndex,
        job_material_id: str,
        layer_id: str,
    ) -> tuple[bytes, str]:
        validate_identifier(job_material_id)
        validate_identifier(layer_id)
        preparations = self.read_gerber_preparations(project_id, run_id, artifact_index)
        preparation = next(
            (item for item in preparations if item.job_material_id == job_material_id), None
        )
        if preparation is None or layer_id not in {
            item.layer_id for item in preparation.layer_inputs
        }:
            raise ServiceError("gerber_preview_not_found")
        expected = f"gerberscout/{job_material_id}/preview/{layer_id}.svg"
        entries = [
            entry
            for entry in artifact_index.entries
            if entry.kind == "gerber_preview" and entry.path == expected
        ]
        if len(entries) != 1 or entries[0].source_material_ids != [job_material_id]:
            raise ServiceError("gerber_preview_not_found")
        return self.read_project_scoped_content(
            project_id, run_id, entry=entries[0], expected_path=expected
        )

    def read_layer_definitions(
        self, project_id: str, run_id: str, artifact_index: ArtifactIndex
    ) -> list[LayerDefinition]:
        if artifact_index.run_id != run_id:
            raise ServiceError("artifact_schema_invalid")
        entries = [entry for entry in artifact_index.entries if entry.kind == "layer_definitions"]
        if not entries:
            return []
        if len(entries) != 1:
            raise ServiceError("layer_definitions_unavailable")
        artifact = self._read_versioned(
            project_id,
            run_id,
            entries[0].path,
            LayerDefinitionsArtifact,
            unavailable_code="layer_definitions_unavailable",
        )
        if any(item.project_id != project_id for item in artifact.definitions):
            raise ServiceError("artifact_schema_invalid")
        snapshot = self._read_indexed_input_snapshot(project_id, run_id, artifact_index)
        by_job = {item.job_material_id: item for item in artifact.definitions}
        if any(job_id not in snapshot.groups.gerber_jobs for job_id in by_job):
            raise ServiceError("artifact_schema_invalid")
        return [
            by_job[job_id].model_copy(deep=True)
            for job_id in snapshot.groups.gerber_jobs
            if job_id in by_job
        ]
