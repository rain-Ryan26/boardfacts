from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from pydantic import ValidationError

from flyboard.errors import ServiceError
from flyboard.models import (
    ArtifactCleanupResult,
    ProjectDeletionResult,
    ProjectRecord,
    ProjectSummary,
    RunStatus,
    materialize_run_summary,
    to_project_summary,
)
from flyboard.repositories import ArtifactRepository, ProjectRepository
from flyboard.services._support import new_id, utc_now


class ProjectService:
    def __init__(
        self,
        repository: ProjectRepository,
        artifacts: ArtifactRepository,
        *,
        clock: Callable[[], datetime] = utc_now,
        project_id_factory: Callable[[], str] = lambda: new_id("prj"),
    ) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self.clock = clock
        self.project_id_factory = project_id_factory

    def create_project(self, name: str) -> ProjectSummary:
        if not isinstance(name, str) or not name.strip():
            raise ServiceError("invalid_project_name")
        normalized_name = name.strip()
        now = self.clock()
        for _ in range(100):
            project_id = self.project_id_factory()
            try:
                record = ProjectRecord(
                    project_id=project_id,
                    project_name=normalized_name,
                    created_at=now,
                    updated_at=now,
                    materials=[],
                    latest_run_id=None,
                )
            except ValidationError as exc:
                raise ServiceError("invalid_project_name") from exc
            try:
                with self.repository.mutate(project_id) as mutation:
                    mutation.create(record)
            except ServiceError as exc:
                if exc.code == "project_already_exists":
                    continue
                raise
            return to_project_summary(record)
        raise ServiceError("project_id_allocation_failed")

    def list_projects(self) -> list[ProjectSummary]:
        return [to_project_summary(record) for record in self.repository.list_records()]

    def get_materials(self, project_id: str):
        record = self.repository.get(project_id)
        return [material.model_copy(deep=True) for material in record.materials]

    def delete_project(self, project_id: str, delete_artifacts: bool) -> ProjectDeletionResult:
        with self.repository.mutate(project_id) as mutation:
            mutation.get_current()
            summaries = self.artifacts.list_run_summaries(project_id)
            active = next(
                (
                    summary
                    for summary in summaries
                    if summary.status in {RunStatus.QUEUED, RunStatus.RUNNING}
                ),
                None,
            )
            if active is not None:
                raise ServiceError(
                    "active_run_exists",
                    active_project_id=project_id,
                    active_run_id=active.run_id,
                    run_summary=materialize_run_summary(active, now=self.clock()),
                )
            mutation.remove(cleanup_mode="delete" if delete_artifacts else "retain")
        cleanup_status = "not_requested"
        if delete_artifacts:
            cleanup_status = (
                "completed" if self.repository.cleanup_deleted_project(project_id) else "pending"
            )
        return ProjectDeletionResult(
            project_id=project_id,
            status="deleted",
            artifact_cleanup=ArtifactCleanupResult(status=cleanup_status),
        )
