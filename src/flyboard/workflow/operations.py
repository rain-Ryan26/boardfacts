from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Executor
from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from flyboard.errors import ServiceError
from flyboard.model_calls import (
    ModelCallFailed,
    ModelCallOutcome,
    aggregate_model_call_summaries,
)
from flyboard.models import (
    ArtifactIndex,
    ArtifactIndexEntry,
    BranchCode,
    ErrorRecord,
    PhaseCode,
    ProjectDataListArtifact,
    RunSummary,
    Severity,
    ToolFinding,
    ToolFindingKind,
    TrajectoryEvent,
    TrajectoryEventType,
)
from flyboard.repositories import ArtifactRepository

from .commit import CommitContractError, RunCommitContext
from .session import RunExecutionSession


def _validated_summary(summary: RunSummary, **updates: object) -> RunSummary:
    values = summary.model_dump(mode="python")
    values.update(updates)
    return RunSummary.model_validate(values)


def _model_outcome_event_summary(outcome: ModelCallOutcome) -> dict[str, object]:
    usage = outcome.usage
    return {
        "call_purpose": outcome.call_purpose.value,
        "outcome_status": outcome.status,
        "failure_category": (
            outcome.failure_category.value if isinstance(outcome, ModelCallFailed) else None
        ),
        "capacity_decision": outcome.capacity_decision.value,
        "policy_version": outcome.policy_version,
        "request_model_id": outcome.request_model_id,
        "response_model_id": outcome.response_model_id,
        "transport_attempt_count": outcome.transport_attempt_count,
        "transport_retried": outcome.transport_retried,
        "provider_request_id": outcome.provider_request_id,
        "finish_reason": outcome.finish_reason,
        "input_tokens": usage.input_tokens if usage is not None else None,
        "output_tokens": usage.output_tokens if usage is not None else None,
        "total_tokens": usage.total_tokens if usage is not None else None,
    }


class RunCommitOps:
    """All in-commit primitives for one session; none of these methods acquire the channel."""

    def __init__(
        self,
        session: RunExecutionSession,
        artifacts: ArtifactRepository,
        clock: Callable[[], datetime],
        *,
        uuid_factory: Callable[[], object] = uuid4,
    ) -> None:
        self.session = session
        self.artifacts = artifacts
        self.clock = clock
        self.uuid_factory = uuid_factory
        self._errors: dict[str, ErrorRecord] = {}
        self._findings: dict[str, ToolFinding] = {}
        self._recorded_model_outcomes: list[ModelCallOutcome] = []

    def _check(self, context: RunCommitContext) -> None:
        self.session.channel.assert_context(context)

    def _next_id(self, prefix: str, existing: dict[str, object]) -> str:
        for _ in range(100):
            candidate = f"{prefix}_{str(self.uuid_factory()).lower()}"
            if candidate not in existing:
                return candidate
        raise CommitContractError("diagnostic id factory did not produce a unique UUID")

    def record_error(
        self,
        context: RunCommitContext,
        *,
        code: str,
        severity: str | Severity,
        phase: str | PhaseCode | None,
        branch: str | BranchCode | None,
        message: str,
        source_material_ids: list[str],
        artifact_paths: list[str],
        suggested_action: str,
    ) -> ErrorRecord:
        self._check(context)
        error_id = self._next_id("err", self._errors)
        try:
            record = ErrorRecord(
                error_id=error_id,
                code=code,
                severity=severity,
                phase=phase,
                branch=branch,
                message=message,
                source_material_ids=source_material_ids,
                artifact_paths=artifact_paths,
                suggested_action=suggested_action,
            )
        except ValidationError as exc:
            raise CommitContractError("invalid diagnostic error payload") from exc
        self._errors[error_id] = record
        return record

    def record_finding(
        self,
        context: RunCommitContext,
        *,
        producer: str,
        code: str,
        kind: str | ToolFindingKind,
        message: str,
        source_material_ids: list[str],
        evidence_ids: list[str],
        error_id: str | None,
    ) -> ToolFinding:
        self._check(context)
        if error_id is not None:
            self.resolve_error(context, error_id)
        finding_id = self._next_id("finding", self._findings)
        try:
            finding = ToolFinding(
                finding_id=finding_id,
                producer=producer,
                code=code,
                kind=kind,
                message=message,
                source_material_ids=source_material_ids,
                evidence_ids=evidence_ids,
                error_id=error_id,
            )
        except ValidationError as exc:
            raise CommitContractError("invalid tool finding payload") from exc
        self._findings[finding_id] = finding
        return finding

    def resolve_error(self, context: RunCommitContext, error_id: str) -> ErrorRecord:
        self._check(context)
        try:
            return self._errors[error_id]
        except KeyError as exc:
            raise CommitContractError("error id does not belong to this run recorder") from exc

    def resolve_finding(self, context: RunCommitContext, finding_id: str) -> ToolFinding:
        self._check(context)
        try:
            return self._findings[finding_id]
        except KeyError as exc:
            raise CommitContractError("finding id does not belong to this run recorder") from exc

    def validate_error_identity(
        self, context: RunCommitContext, record: ErrorRecord
    ) -> ErrorRecord:
        canonical = self.resolve_error(context, record.error_id)
        if canonical is not record:
            raise CommitContractError("diagnostic error object identity was rebuilt")
        return canonical

    def validate_finding_identity(
        self, context: RunCommitContext, finding: ToolFinding
    ) -> ToolFinding:
        canonical = self.resolve_finding(context, finding.finding_id)
        if canonical is not finding:
            raise CommitContractError("tool finding object identity was rebuilt")
        return canonical

    def all_errors(self, context: RunCommitContext) -> list[ErrorRecord]:
        self._check(context)
        return list(self._errors.values())

    def submit_worker(
        self,
        context: RunCommitContext,
        executor: Executor,
        *,
        task_key: str,
        worker: Callable[[], object],
    ) -> bool:
        self._check(context)
        if not self.session.control.reserve_task(task_key):
            return False
        try:
            handle = executor.submit(worker)
        except Exception:
            self.session.control.release_task(task_key)
            raise
        self.session.control.bind_task(task_key, handle)
        return True

    def persist_summary(self, context: RunCommitContext, summary: RunSummary) -> RunSummary:
        self._check(context)
        try:
            self.artifacts.replace_run_summary(
                self.session.project_id, self.session.run_id, summary
            )
        except ServiceError:
            self.session.control.close_business()
            raise
        self.session.summary = summary
        return summary

    def add_errors_to_summary(
        self, context: RunCommitContext, errors: list[ErrorRecord]
    ) -> RunSummary:
        self._check(context)
        existing = {item.error_id for item in self.session.summary.error_summary}
        merged = list(self.session.summary.error_summary)
        for error in errors:
            self.validate_error_identity(context, error)
            if error.error_id not in existing:
                merged.append(error)
                existing.add(error.error_id)
        next_summary = _validated_summary(self.session.summary, error_summary=merged)
        return self.persist_summary(context, next_summary)

    def record_model_call(
        self,
        context: RunCommitContext,
        outcome: ModelCallOutcome,
        *,
        phase: str | PhaseCode,
        branch: str | BranchCode | None,
        error_id: str | None,
        artifact_paths: list[str],
    ) -> None:
        self._check(context)
        if any(recorded is outcome for recorded in self._recorded_model_outcomes):
            raise CommitContractError("model call outcome object was already recorded")
        if len(artifact_paths) != len(set(artifact_paths)):
            raise CommitContractError("model call artifact paths must be unique")
        missing_paths = [
            path for path in artifact_paths if path not in self.session.committed_artifact_paths
        ]
        if missing_paths:
            raise CommitContractError("model call artifact path was not committed by this session")
        if error_id is not None:
            self.resolve_error(context, error_id)

        next_summaries = aggregate_model_call_summaries(
            self.session.summary.model_call_summaries, outcome
        )
        if not isinstance(outcome, ModelCallFailed):
            try:
                event = TrajectoryEvent(
                    schema_version="1.0",
                    run_id=self.session.run_id,
                    timestamp=self.clock(),
                    phase=phase,
                    branch=branch,
                    event_type=TrajectoryEventType.MODEL_RESPONSE_RECEIVED,
                    summary=_model_outcome_event_summary(outcome),
                    duration_ms=outcome.duration_ms,
                    source_material_ids=[],
                    artifact_paths=list(artifact_paths),
                    error_ids=[error_id] if error_id is not None else [],
                )
            except ValidationError as exc:
                raise CommitContractError("invalid model call recorder payload") from exc
            self.append_trajectory(context, event)
        next_summary = _validated_summary(self.session.summary, model_call_summaries=next_summaries)
        self.persist_summary(context, next_summary)
        self._recorded_model_outcomes.append(outcome)

    def append_trajectory(self, context: RunCommitContext, event: TrajectoryEvent) -> None:
        self._check(context)
        for error_id in event.error_ids:
            self.resolve_error(context, error_id)
        try:
            self.artifacts.append_trajectory(self.session.project_id, self.session.run_id, event)
        except ServiceError as exc:
            error = self.record_error(
                context,
                code="run_record_write_failed",
                severity=Severity.ERROR,
                phase=event.phase,
                branch=event.branch,
                message="运行轨迹记录未能完整写入。",
                source_material_ids=list(event.source_material_ids),
                artifact_paths=[],
                suggested_action="检查受管存储后创建新的运行。",
            )
            try:
                self.add_errors_to_summary(context, [error])
            finally:
                self.session.control.close_business()
            raise ServiceError("run_record_write_failed", error_id=error.error_id) from exc

    def _artifact_event(
        self,
        context: RunCommitContext,
        *,
        entry: ArtifactIndexEntry,
        branch: BranchCode | None,
        indexed: bool,
        error_ids: list[str],
    ) -> None:
        event = TrajectoryEvent(
            schema_version="1.0",
            run_id=self.session.run_id,
            timestamp=self.clock(),
            phase=entry.producer_phase,
            branch=branch,
            event_type=TrajectoryEventType.ARTIFACT_WRITTEN,
            summary={"kind": entry.kind, "indexed": indexed},
            source_material_ids=list(entry.source_material_ids),
            artifact_paths=[entry.path],
            error_ids=error_ids,
        )
        self.append_trajectory(context, event)

    def _record_index_failure(
        self,
        context: RunCommitContext,
        *,
        entry: ArtifactIndexEntry,
        branch: BranchCode | None,
    ) -> ErrorRecord:
        if self.session.artifact_index_error is not None:
            return self.session.artifact_index_error
        error = self.record_error(
            context,
            code="artifact_index_update_failed",
            severity=Severity.ERROR,
            phase=entry.producer_phase,
            branch=branch,
            message="产物已经写出，但运行产物索引更新失败。",
            source_material_ids=list(entry.source_material_ids),
            artifact_paths=[entry.path],
            suggested_action="保留本次诊断文件并创建新的运行。",
        )
        self.session.artifact_index_available = False
        self.session.artifact_index_error = error
        next_summary = _validated_summary(
            self.session.summary,
            artifact_index_path=None,
            error_summary=[*self.session.summary.error_summary, error],
        )
        self.persist_summary(context, next_summary)
        return error

    def write_json(
        self,
        context: RunCommitContext,
        artifact: BaseModel,
        entry: ArtifactIndexEntry,
        *,
        branch: BranchCode | None = None,
        target_failure_error_id: str | None = None,
        target_error_code: str = "artifact_write_failed",
        on_index_commit: Callable[[str], None] | None = None,
    ) -> str:
        self._check(context)
        if not self.session.control.accepts_business():
            raise ServiceError("run_write_closed")
        shared_error = (
            self.resolve_error(context, target_failure_error_id)
            if target_failure_error_id is not None
            else None
        )
        try:
            path = self.artifacts.replace_json_artifact(
                self.session.project_id,
                self.session.run_id,
                artifact=artifact,
                entry=entry,
            )
        except ServiceError as exc:
            if exc.code != "artifact_write_failed":
                raise
            if shared_error is not None:
                raise ServiceError(target_error_code, error_id=shared_error.error_id) from exc
            error = self.record_error(
                context,
                code=target_error_code,
                severity=Severity.ERROR,
                phase=entry.producer_phase,
                branch=branch,
                message="运行产物未能完成原子写入。",
                source_material_ids=list(entry.source_material_ids),
                artifact_paths=[],
                suggested_action="检查受管存储后创建新的运行。",
            )
            raise ServiceError(target_error_code, error_id=error.error_id) from exc

        self.session.committed_artifact_paths.add(path)

        index_error: ErrorRecord | None = None
        if self.session.artifact_index_available:
            entries = list(self.session.artifact_index.entries)
            replacement = next(
                (index for index, item in enumerate(entries) if item.path == entry.path), None
            )
            if replacement is None:
                entries.append(entry)
            else:
                entries[replacement] = entry
            next_index = ArtifactIndex(
                schema_version="1.0", run_id=self.session.run_id, entries=entries
            )
            try:
                self.artifacts.replace_artifact_index(
                    self.session.project_id, self.session.run_id, next_index
                )
            except ServiceError:
                index_error = self._record_index_failure(context, entry=entry, branch=branch)
            else:
                self.session.artifact_index = next_index

        if isinstance(artifact, ProjectDataListArtifact):
            next_summary = _validated_summary(self.session.summary, project_data_list_path=path)
            self.persist_summary(context, next_summary)

        indexed = index_error is None and self.session.artifact_index_available
        if indexed and on_index_commit is not None:
            on_index_commit(path)
        effective_index_error = index_error or (
            self.session.artifact_index_error if not self.session.artifact_index_available else None
        )
        self._artifact_event(
            context,
            entry=entry,
            branch=branch,
            indexed=indexed,
            error_ids=(
                [effective_index_error.error_id] if effective_index_error is not None else []
            ),
        )
        if index_error is not None:
            raise ServiceError("artifact_index_update_failed", error_id=index_error.error_id)
        if on_index_commit is not None and not indexed:
            if effective_index_error is None:
                raise CommitContractError(
                    "indexed artifact commit did not produce an index outcome"
                )
            raise ServiceError(
                "artifact_index_update_failed",
                error_id=effective_index_error.error_id,
            )
        return path

    def write_content(
        self,
        context: RunCommitContext,
        content: bytes,
        media_type: str,
        entry: ArtifactIndexEntry,
        *,
        branch: BranchCode | None = None,
        optional_failure_code: Literal["evidence_highlight_failed"] | None = None,
    ) -> str:
        self._check(context)
        if optional_failure_code not in {None, "evidence_highlight_failed"}:
            raise CommitContractError("unsupported optional content failure code")
        if not self.session.control.accepts_business():
            raise ServiceError("run_write_closed")
        try:
            path = self.artifacts.replace_content_artifact(
                self.session.project_id,
                self.session.run_id,
                content=content,
                media_type=media_type,
                entry=entry,
            )
        except ServiceError as exc:
            if exc.code != "artifact_write_failed":
                raise
            error_code = optional_failure_code or "artifact_write_failed"
            is_optional = optional_failure_code is not None
            error = self.record_error(
                context,
                code=error_code,
                severity=Severity.WARNING if is_optional else Severity.ERROR,
                phase=entry.producer_phase,
                branch=branch,
                message=(
                    "证据高亮图写入失败，已降级为页图或整图。"
                    if is_optional
                    else "运行内容产物未能完成原子写入。"
                ),
                source_material_ids=list(entry.source_material_ids),
                artifact_paths=[],
                suggested_action=(
                    "通过原始页图或整图复核该证据。"
                    if is_optional
                    else "检查受管存储后创建新的运行。"
                ),
            )
            raise ServiceError(error_code, error_id=error.error_id) from exc

        self.session.committed_artifact_paths.add(path)

        index_error: ErrorRecord | None = None
        if self.session.artifact_index_available:
            entries = list(self.session.artifact_index.entries)
            entries = [item for item in entries if item.path != entry.path]
            entries.append(entry)
            next_index = ArtifactIndex(
                schema_version="1.0", run_id=self.session.run_id, entries=entries
            )
            try:
                self.artifacts.replace_artifact_index(
                    self.session.project_id, self.session.run_id, next_index
                )
            except ServiceError:
                index_error = self._record_index_failure(context, entry=entry, branch=branch)
            else:
                self.session.artifact_index = next_index
        effective = index_error or (
            self.session.artifact_index_error if not self.session.artifact_index_available else None
        )
        self._artifact_event(
            context,
            entry=entry,
            branch=branch,
            indexed=effective is None,
            error_ids=[effective.error_id] if effective is not None else [],
        )
        if index_error is not None:
            raise ServiceError("artifact_index_update_failed", error_id=index_error.error_id)
        return path


class DiagnosticRecorder:
    """Lock-external diagnostic facade bound to one run commit channel."""

    def __init__(self, session: RunExecutionSession, ops: RunCommitOps) -> None:
        self._session = session
        self._ops = ops

    def record_error(
        self,
        *,
        code: str,
        severity: str,
        phase: str | None,
        branch: str | None,
        message: str,
        source_material_ids: list[str],
        artifact_paths: list[str],
        suggested_action: str,
    ) -> ErrorRecord:
        return self._session.channel.execute(
            lambda context: self._ops.record_error(
                context,
                code=code,
                severity=severity,
                phase=phase,
                branch=branch,
                message=message,
                source_material_ids=source_material_ids,
                artifact_paths=artifact_paths,
                suggested_action=suggested_action,
            )
        )

    def record_finding(
        self,
        *,
        producer: str,
        code: str,
        kind: str,
        message: str,
        source_material_ids: list[str],
        evidence_ids: list[str],
        error_id: str | None,
    ) -> ToolFinding:
        return self._session.channel.execute(
            lambda context: self._ops.record_finding(
                context,
                producer=producer,
                code=code,
                kind=kind,
                message=message,
                source_material_ids=source_material_ids,
                evidence_ids=evidence_ids,
                error_id=error_id,
            )
        )

    def resolve_error(self, error_id: str) -> ErrorRecord:
        return self._session.channel.execute(
            lambda context: self._ops.resolve_error(context, error_id)
        )

    def resolve_finding(self, finding_id: str) -> ToolFinding:
        return self._session.channel.execute(
            lambda context: self._ops.resolve_finding(context, finding_id)
        )


class ModelCallRecorder:
    """Lock-external model outcome recorder bound to one run commit channel."""

    def __init__(self, session: RunExecutionSession, ops: RunCommitOps) -> None:
        self._session = session
        self._ops = ops

    def record(
        self,
        outcome: ModelCallOutcome,
        *,
        phase: str | PhaseCode,
        branch: str | BranchCode | None,
        error_id: str | None = None,
        artifact_paths: list[str] | None = None,
    ) -> None:
        paths = list(artifact_paths or [])
        self._session.channel.execute(
            lambda context: self._ops.record_model_call(
                context,
                outcome,
                phase=phase,
                branch=branch,
                error_id=error_id,
                artifact_paths=paths,
            )
        )


class ArtifactWriter:
    """Lock-external artifact facade with an optional fixed branch scope."""

    def __init__(
        self,
        session: RunExecutionSession,
        ops: RunCommitOps,
        *,
        branch: BranchCode | None = None,
    ) -> None:
        self._session = session
        self._ops = ops
        self._branch = branch

    def for_branch(self, branch: BranchCode) -> ArtifactWriter:
        return ArtifactWriter(self._session, self._ops, branch=branch)

    def write_json(
        self,
        artifact: BaseModel,
        entry: ArtifactIndexEntry,
        *,
        target_failure_error_id: str | None = None,
    ) -> str:
        return self._session.channel.execute(
            lambda context: self._ops.write_json(
                context,
                artifact,
                entry,
                branch=self._branch,
                target_failure_error_id=target_failure_error_id,
            )
        )

    def write_content(
        self,
        content: bytes,
        media_type: str,
        entry: ArtifactIndexEntry,
        *,
        optional_failure_code: Literal["evidence_highlight_failed"] | None = None,
    ) -> str:
        return self._session.channel.execute(
            lambda context: self._ops.write_content(
                context,
                content,
                media_type,
                entry,
                branch=self._branch,
                optional_failure_code=optional_failure_code,
            )
        )


class TrajectoryWriter:
    """Lock-external public-event writer bound to one run."""

    def __init__(self, session: RunExecutionSession, ops: RunCommitOps) -> None:
        self._session = session
        self._ops = ops

    def append(self, event: TrajectoryEvent) -> None:
        self._session.channel.execute(lambda context: self._ops.append_trajectory(context, event))
