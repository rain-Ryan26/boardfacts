from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from flyboard.config import AppConfig
from flyboard.model_calls import ModelCaller
from flyboard.models import (
    ArtifactIndex,
    BranchCode,
    BranchResult,
    BranchResultStatus,
    BranchStatus,
    BranchStatusMap,
    ErrorRecord,
    EvidenceIndex,
    EvidenceMetadataResponse,
    GerberJobPreparation,
    IntegrationResult,
    LayerDefinition,
    PhaseCode,
    PhaseTiming,
    PhaseTimingStatus,
    ProjectDataRow,
    Region,
    RuleCheckResult,
    RuleCheckStatus,
    RuleCheckSummary,
    RuleSetSnapshot,
    RunInputSnapshot,
    RunStatus,
    RunSummary,
    ScopeType,
    ToolFinding,
)
from flyboard.workflow.control import StopSignal
from flyboard.workflow.operations import (
    ArtifactWriter,
    DiagnosticRecorder,
    ModelCallRecorder,
    TrajectoryWriter,
)

TERMINAL_BRANCH_STATUSES = {
    BranchStatus.SKIPPED,
    BranchStatus.STOPPED,
    BranchStatus.COMPLETED,
    BranchStatus.COMPLETED_WITH_WARNINGS,
    BranchStatus.FAILED,
}
RESULT_TO_BRANCH = {
    BranchResultStatus.COMPLETED: BranchStatus.COMPLETED,
    BranchResultStatus.COMPLETED_WITH_WARNINGS: BranchStatus.COMPLETED_WITH_WARNINGS,
    BranchResultStatus.FAILED: BranchStatus.FAILED,
}
BRANCH_TO_ATTR = {
    BranchCode.PDF_IMAGE: "pdf_image",
    BranchCode.XLSX: "xlsx",
    BranchCode.GERBER: "gerber",
}
BRANCH_ORDER = (BranchCode.PDF_IMAGE, BranchCode.XLSX, BranchCode.GERBER)


class EvidenceIdHelper(Protocol):
    def assign(
        self,
        *,
        evidence_type: str,
        source_material_id: str,
        file_path: str,
        page_index: int | None,
        region: Region | None,
        source_asset_path: str | None = None,
        source_image_index: int | None = None,
        artifact_kind: str | None = None,
        artifact_path: str | None = None,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class AnalysisContext:
    project_id: str
    run_id: str
    project_root: Path
    output_dir: Path
    config: AppConfig
    stop_signal: StopSignal
    artifact_writer: ArtifactWriter
    trajectory_writer: TrajectoryWriter
    diagnostics: DiagnosticRecorder
    model_client: ModelCaller
    model_calls: ModelCallRecorder
    evidence_id_helper: EvidenceIdHelper


BranchWorker = Callable[[RunInputSnapshot, AnalysisContext], BranchResult]


class GerberAnalyzer(Protocol):
    def prepare(
        self,
        snapshot: RunInputSnapshot,
        job_material_id: str,
        context: AnalysisContext,
    ) -> tuple[GerberJobPreparation | None, list[ErrorRecord]]: ...

    def analyze(
        self,
        snapshot: RunInputSnapshot,
        context: AnalysisContext,
        preparations: list[GerberJobPreparation],
        layer_definitions: list[LayerDefinition],
    ) -> BranchResult: ...


class Integrator(Protocol):
    def merge(
        self,
        snapshot: RunInputSnapshot,
        branch_results: list[BranchResult],
        tool_findings: list[ToolFinding],
        context: AnalysisContext,
    ) -> IntegrationResult: ...


class RuleEngine(Protocol):
    def check(
        self,
        snapshot: RunInputSnapshot,
        integration_result: IntegrationResult,
        rule_set_snapshot: RuleSetSnapshot | None,
        context: AnalysisContext,
    ) -> RuleCheckResult: ...


class EvidenceService(Protocol):
    def build_index(
        self,
        branch_results: list[BranchResult],
        integration_result: IntegrationResult,
        context: AnalysisContext,
    ) -> EvidenceIndex | None: ...

    def get(
        self,
        project_id: str,
        run_id: str,
        evidence_index: EvidenceIndex,
        evidence_id: str,
    ) -> EvidenceMetadataResponse: ...

    def get_content(
        self,
        project_id: str,
        run_id: str,
        artifact_index: ArtifactIndex,
        evidence_index: EvidenceIndex,
        evidence_id: str,
    ) -> tuple[bytes, str]: ...


class ReportRenderer(Protocol):
    def render(
        self,
        run_id: str,
        project_data_list: list[ProjectDataRow],
        rule_check_result: RuleCheckResult | None,
        tool_findings: list[ToolFinding] | None,
        evidence_index: EvidenceIndex | None,
    ) -> str: ...


def duration_ms(started_at: datetime, finished_at: datetime) -> int:
    return max(0, int((finished_at - started_at).total_seconds() * 1000))


def branch_sources(snapshot: RunInputSnapshot, branch: BranchCode) -> list[str]:
    if branch == BranchCode.PDF_IMAGE:
        selected = set(snapshot.groups.pdf_inputs + snapshot.groups.image_inputs)
        return [
            material.material_id
            for material in snapshot.materials
            if material.material_id in selected
        ]
    if branch == BranchCode.XLSX:
        return list(snapshot.groups.spreadsheet_inputs)
    return list(snapshot.groups.gerber_jobs)


def replace_branch_status(
    status_map: BranchStatusMap, branch: BranchCode, status: BranchStatus
) -> BranchStatusMap:
    values = status_map.model_dump(mode="python")
    values[BRANCH_TO_ATTR[branch]] = status
    return BranchStatusMap.model_validate(values)


def finish_running_timings(
    timings: list[PhaseTiming],
    *,
    finished_at: datetime,
    status: PhaseTimingStatus,
) -> list[PhaseTiming]:
    result: list[PhaseTiming] = []
    for timing in timings:
        if timing.status != PhaseTimingStatus.RUNNING:
            result.append(timing)
            continue
        result.append(
            PhaseTiming(
                phase=timing.phase,
                scope_type=timing.scope_type,
                scope_id=timing.scope_id,
                status=status,
                started_at=timing.started_at,
                finished_at=finished_at,
                duration_ms=duration_ms(timing.started_at, finished_at),
            )
        )
    return result


def terminal_branch_map(current: BranchStatusMap, *, stopped: bool) -> BranchStatusMap:
    target = BranchStatus.STOPPED if stopped else BranchStatus.FAILED
    values: dict[str, BranchStatus] = {}
    for attr in ("pdf_image", "xlsx", "gerber"):
        value = getattr(current, attr)
        values[attr] = value if value in TERMINAL_BRANCH_STATUSES else target
    return BranchStatusMap(**values)


def terminal_rule_summary(
    current: RuleCheckSummary, *, stopped: bool, service_interrupted: bool
) -> RuleCheckSummary:
    if current.status in {
        RuleCheckStatus.SKIPPED,
        RuleCheckStatus.COMPLETED,
        RuleCheckStatus.COMPLETED_WITH_ERRORS,
        RuleCheckStatus.INTERRUPTED,
    }:
        return current
    if stopped:
        reason = "run_stopped"
    elif service_interrupted:
        reason = "service_interrupted"
    elif current.status == RuleCheckStatus.NOT_REACHED:
        reason = "upstream_failed"
    else:
        reason = "rule_check_failed"
    if current.status == RuleCheckStatus.NOT_REACHED:
        return RuleCheckSummary(
            status=RuleCheckStatus.NOT_REACHED,
            rule_set_id=current.rule_set_id,
            rule_set_version=current.rule_set_version,
            attempted_rule_count=0,
            error_count=0,
            reason=reason,
        )
    return RuleCheckSummary(
        status=RuleCheckStatus.INTERRUPTED,
        rule_set_id=current.rule_set_id,
        rule_set_version=current.rule_set_version,
        attempted_rule_count=None,
        error_count=None,
        reason=reason,
    )


def terminal_summary(
    summary: RunSummary,
    *,
    status: RunStatus,
    finished_at: datetime,
    errors: list[ErrorRecord] | None = None,
    service_interrupted: bool = False,
    stop_requested_at: datetime | None = None,
) -> RunSummary:
    from flyboard.workflow.operations import _validated_summary

    stopped = status == RunStatus.STOPPED
    merged = list(summary.error_summary)
    seen = {item.error_id for item in merged}
    for error in errors or []:
        if error.error_id not in seen:
            merged.append(error)
            seen.add(error.error_id)
    return _validated_summary(
        summary,
        status=status,
        phase_timings=finish_running_timings(
            summary.phase_timings,
            finished_at=finished_at,
            status=PhaseTimingStatus.STOPPED if stopped else PhaseTimingStatus.FAILED,
        ),
        branch_status=terminal_branch_map(summary.branch_status, stopped=stopped),
        rule_check_summary=terminal_rule_summary(
            summary.rule_check_summary,
            stopped=stopped,
            service_interrupted=service_interrupted,
        ),
        error_summary=merged,
        stop_requested_at=stop_requested_at or summary.stop_requested_at,
        stop_reason="user_requested" if stopped else None,
        finished_at=finished_at,
        duration_ms=duration_ms(summary.started_at, finished_at),
    )


def skipped_branch_timing(branch: BranchCode, now: datetime) -> PhaseTiming:
    return PhaseTiming(
        phase=PhaseCode.BRANCH_ANALYSIS,
        scope_type=ScopeType.BRANCH,
        scope_id=branch.value,
        status=PhaseTimingStatus.SKIPPED,
        started_at=now,
        finished_at=now,
        duration_ms=0,
    )
