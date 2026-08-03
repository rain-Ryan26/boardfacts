from __future__ import annotations

from flyboard.errors import ServiceError
from flyboard.models import (
    DataListUnavailableSummary,
    ProjectDataRow,
    RunStatus,
    RunSummaryResponse,
    ToolFinding,
    materialize_run_summary,
    to_error_summary,
)


class ReadCommandsMixin:
    def get_summary(self, project_id: str, run_id: str) -> RunSummaryResponse:
        with self.projects.guard_product_run(project_id, run_id):
            summary = self.artifacts.read_run_summary(project_id, run_id)
            return materialize_run_summary(summary, now=self.clock())

    @staticmethod
    def _unavailable_summary(project_id, summary) -> DataListUnavailableSummary:
        reason: str | None = None
        if summary.status == RunStatus.STOPPED:
            reason = "user_requested"
        elif summary.status == RunStatus.FAILED and summary.error_summary:
            no_processable = next(
                (
                    error.code
                    for error in summary.error_summary
                    if error.code == "no_processable_materials"
                ),
                None,
            )
            reason = no_processable or summary.error_summary[0].code
        return DataListUnavailableSummary(
            kind="data_list_unavailable",
            run_status=summary.status.value,
            reason_code=reason,
            current_phase=summary.current_phase,
            branch_status=summary.branch_status,
            rule_check_summary=summary.rule_check_summary,
            error_summary=[to_error_summary(error) for error in summary.error_summary],
            stop_requested_at=summary.stop_requested_at,
            stop_reason=summary.stop_reason,
            run_summary_endpoint=f"/projects/{project_id}/runs/{summary.run_id}",
        )

    def get_data_list_view(
        self, project_id: str, run_id: str
    ) -> list[ProjectDataRow] | DataListUnavailableSummary:
        with self.projects.guard_product_run(project_id, run_id):
            summary = self.artifacts.read_run_summary(project_id, run_id)
            if summary.status not in {
                RunStatus.COMPLETED,
                RunStatus.COMPLETED_WITH_ERRORS,
            }:
                return self._unavailable_summary(project_id, summary)
            locator = summary.project_data_list_path
            if locator is None:
                raise ServiceError("project_data_list_unavailable")
            if summary.artifact_index_path is None:
                return self.artifacts.read_fallback_project_data_list(project_id, run_id, locator)
            try:
                index = self.artifacts.read_artifact_index(
                    project_id, run_id, summary.artifact_index_path
                )
            except ServiceError as exc:
                if exc.code == "artifact_index_unavailable":
                    raise ServiceError("project_data_list_unavailable") from exc
                raise
            entries = [entry for entry in index.entries if entry.kind == "project_data_list"]
            if len(entries) != 1 or entries[0].path != locator:
                raise ServiceError("project_data_list_unavailable")
            return self.artifacts.read_indexed_project_data_list(project_id, run_id, entries[0])

    def get_tool_findings_view(self, project_id: str, run_id: str) -> list[ToolFinding]:
        with self.projects.guard_product_run(project_id, run_id):
            summary = self.artifacts.read_run_summary(project_id, run_id)
            session = self._registry.get(project_id, run_id)
            if session is not None:
                if session.tool_findings is None:
                    raise ServiceError("tool_findings_not_ready")
            elif summary.status in {RunStatus.QUEUED, RunStatus.RUNNING} or any(
                error.code
                in {
                    "no_processable_materials",
                    "run_creation_failed",
                    "run_dispatch_failed",
                    "service_interrupted",
                    "branch_result_write_failed",
                }
                for error in summary.error_summary
            ):
                raise ServiceError("tool_findings_not_ready")
            if summary.artifact_index_path is None:
                raise ServiceError("artifact_index_unavailable")
            index = self.artifacts.read_artifact_index(
                project_id, run_id, summary.artifact_index_path
            )
            entries = [entry for entry in index.entries if entry.kind == "tool_findings"]
            if len(entries) != 1:
                raise ServiceError("tool_findings_unavailable")
            return self.artifacts.read_tool_findings(project_id, run_id, entries[0])
