from __future__ import annotations

from flyboard.errors import ServiceError
from flyboard.models import (
    BranchCode,
    BranchStatus,
    ErrorRecord,
    GerberJobPreparation,
    PhaseCode,
    PhaseTiming,
    PhaseTimingStatus,
    RunStatus,
    ScopeType,
    Severity,
    TrajectoryEvent,
    TrajectoryEventType,
)

from ._state import duration_ms, replace_branch_status
from .commit import CommitContractError, RunCommitContext
from .control import RunStopRequested
from .operations import _validated_summary


class GerberPreparationMixin:
    def _activate_gerber_preparations(self, session) -> None:
        if session.snapshot is None or self.gerber_analyzer is None:
            return
        for job_material_id in session.snapshot.groups.gerber_jobs:
            task_key = f"gerber_preparation:{job_material_id}"

            def command(context: RunCommitContext, job_id=job_material_id, key=task_key) -> None:
                state = session.preparation_states_by_job[job_id]
                if (
                    state.status != "queued"
                    or session.summary.status != RunStatus.RUNNING
                    or session.summary.stop_requested_at is not None
                    or not session.control.accepts_business()
                ):
                    return
                try:
                    submitted = session.ops.submit_worker(
                        context,
                        self.executor,
                        task_key=key,
                        worker=lambda: self._execute_gerber_preparation(session, job_id, key),
                    )
                except Exception:
                    error = session.ops.record_error(
                        context,
                        code="run_dispatch_failed",
                        severity=Severity.ERROR,
                        phase=PhaseCode.GERBER_PREPARATION,
                        branch=BranchCode.GERBER,
                        message="Gerber preparation worker 未能提交到本地执行器。",
                        source_material_ids=[job_id],
                        artifact_paths=[],
                        suggested_action="检查本地执行资源后创建新的运行。",
                    )
                    state.status = "failed"
                    state.error_ids = [error.error_id]
                    session.ops.add_errors_to_summary(context, [error])
                    self._check_gerber_readiness(context, session)
                    return
                if not submitted:
                    return

            session.channel.execute(command)

    @staticmethod
    def _preparation_artifact_path(job_material_id: str) -> str:
        return f"gerberscout/{job_material_id}/preparation.json"

    @staticmethod
    def _preview_artifact_path(job_material_id: str, layer_id: str) -> str:
        return f"gerberscout/{job_material_id}/preview/{layer_id}.svg"

    def _preparation_artifacts_are_committed(
        self,
        session,
        preparation: GerberJobPreparation,
    ) -> bool:
        if not session.artifact_index_available:
            return False
        by_path = {entry.path: entry for entry in session.artifact_index.entries}
        preparation_entry = by_path.get(
            self._preparation_artifact_path(preparation.job_material_id)
        )
        if (
            preparation_entry is None
            or preparation_entry.kind != "gerber_preparation"
            or preparation_entry.producer_phase != PhaseCode.GERBER_PREPARATION
            or preparation_entry.access_level != "project_scoped"
            or preparation_entry.source_material_ids != [preparation.job_material_id]
        ):
            return False
        for layer in preparation.layer_inputs:
            preview_entry = by_path.get(
                self._preview_artifact_path(preparation.job_material_id, layer.layer_id)
            )
            if (
                preview_entry is None
                or preview_entry.kind != "gerber_preview"
                or preview_entry.producer_phase != PhaseCode.GERBER_PREPARATION
                or preview_entry.access_level != "project_scoped"
                or preview_entry.source_material_ids != [preparation.job_material_id]
            ):
                return False
        return True

    def _begin_gerber_preparation(self, session, job_material_id: str) -> bool:
        def command(context: RunCommitContext) -> bool:
            state = session.preparation_states_by_job[job_material_id]
            if (
                state.status != "queued"
                or session.summary.status != RunStatus.RUNNING
                or session.summary.stop_requested_at is not None
                or not session.control.accepts_business()
            ):
                return False
            now = self.clock()
            if any(
                timing.phase == PhaseCode.GERBER_PREPARATION
                and timing.scope_type == ScopeType.GERBER_JOB
                and timing.scope_id == job_material_id
                for timing in session.summary.phase_timings
            ):
                raise CommitContractError("duplicate Gerber preparation timing")
            timing = PhaseTiming(
                phase=PhaseCode.GERBER_PREPARATION,
                scope_type=ScopeType.GERBER_JOB,
                scope_id=job_material_id,
                status=PhaseTimingStatus.RUNNING,
                started_at=now,
                finished_at=None,
                duration_ms=None,
            )
            state.status = "running"
            session.ops.persist_summary(
                context,
                _validated_summary(
                    session.summary,
                    phase_timings=[*session.summary.phase_timings, timing],
                    branch_status=replace_branch_status(
                        session.summary.branch_status,
                        BranchCode.GERBER,
                        BranchStatus.RUNNING,
                    ),
                ),
            )
            session.ops.append_trajectory(
                context,
                TrajectoryEvent(
                    schema_version="1.0",
                    run_id=session.run_id,
                    timestamp=now,
                    phase=PhaseCode.GERBER_PREPARATION,
                    branch=BranchCode.GERBER,
                    event_type=TrajectoryEventType.PHASE_STARTED,
                    summary={"job_material_id": job_material_id},
                    source_material_ids=[job_material_id],
                    artifact_paths=[],
                    error_ids=[],
                ),
            )
            return True

        return session.channel.execute(command)

    def _resolve_preparation_exception(
        self,
        context: RunCommitContext,
        session,
        job_material_id: str,
        failure: Exception,
    ) -> list[ErrorRecord]:
        if isinstance(failure, ServiceError) and failure.error_id is not None:
            try:
                existing = session.ops.resolve_error(context, failure.error_id)
            except CommitContractError:
                existing = None
            if existing is not None:
                if failure.code != "artifact_index_update_failed":
                    return [existing]
                derived = session.ops.record_error(
                    context,
                    code="gerber_artifact_index_unavailable",
                    severity=Severity.ERROR,
                    phase=PhaseCode.GERBER_PREPARATION,
                    branch=BranchCode.GERBER,
                    message="Gerber preparation 产物索引不可用。",
                    source_material_ids=[job_material_id],
                    artifact_paths=[],
                    suggested_action="保留诊断文件并创建新的运行。",
                )
                return [derived]
        error = session.ops.record_error(
            context,
            code="gerber_input_parse_failed",
            severity=Severity.ERROR,
            phase=PhaseCode.GERBER_PREPARATION,
            branch=BranchCode.GERBER,
            message="Gerber job 未能形成合法 preparation。",
            source_material_ids=[job_material_id],
            artifact_paths=[],
            suggested_action="检查 Gerber job 内容后创建新的运行。",
        )
        return [error]

    def _canonical_preparation_output(
        self,
        context: RunCommitContext,
        session,
        job_material_id: str,
        preparation,
        errors,
    ) -> tuple[GerberJobPreparation | None, list[ErrorRecord]]:
        try:
            if not isinstance(errors, list):
                raise CommitContractError("preparation errors must be a list")
            canonical_errors = [
                session.ops.validate_error_identity(context, error) for error in errors
            ]
            if preparation is None:
                if not any(
                    error.severity == Severity.ERROR
                    and job_material_id in error.source_material_ids
                    for error in canonical_errors
                ):
                    raise CommitContractError("failed preparation requires a job-scoped error")
                return None, canonical_errors
            if not isinstance(preparation, GerberJobPreparation):
                raise CommitContractError("preparation has an invalid type")
            if (
                preparation.project_id != session.project_id
                or preparation.run_id != session.run_id
                or preparation.job_material_id != job_material_id
                or not self._preparation_artifacts_are_committed(session, preparation)
            ):
                raise CommitContractError("preparation identity or committed artifacts are invalid")
            return preparation, canonical_errors
        except (CommitContractError, TypeError, ValueError):
            error = session.ops.record_error(
                context,
                code="gerber_input_parse_failed",
                severity=Severity.ERROR,
                phase=PhaseCode.GERBER_PREPARATION,
                branch=BranchCode.GERBER,
                message="Gerber analyzer 返回的 preparation 未通过运行合同校验。",
                source_material_ids=[job_material_id],
                artifact_paths=[],
                suggested_action="检查 Gerber preparation 实现后创建新的运行。",
            )
            return None, [error]

    def _finish_gerber_preparation(
        self,
        context: RunCommitContext,
        session,
        job_material_id: str,
        preparation: GerberJobPreparation | None,
        errors: list[ErrorRecord],
    ) -> None:
        state = session.preparation_states_by_job[job_material_id]
        if state.status != "running":
            raise CommitContractError("Gerber preparation completion is not running")
        now = self.clock()
        timings: list[PhaseTiming] = []
        final_timing: PhaseTiming | None = None
        succeeded = preparation is not None
        for timing in session.summary.phase_timings:
            if (
                timing.phase == PhaseCode.GERBER_PREPARATION
                and timing.scope_type == ScopeType.GERBER_JOB
                and timing.scope_id == job_material_id
                and timing.status == PhaseTimingStatus.RUNNING
            ):
                final_timing = PhaseTiming(
                    phase=timing.phase,
                    scope_type=timing.scope_type,
                    scope_id=timing.scope_id,
                    status=(
                        PhaseTimingStatus.COMPLETED_WITH_ERRORS
                        if succeeded and any(error.severity == Severity.ERROR for error in errors)
                        else (
                            PhaseTimingStatus.COMPLETED if succeeded else PhaseTimingStatus.FAILED
                        )
                    ),
                    started_at=timing.started_at,
                    finished_at=now,
                    duration_ms=duration_ms(timing.started_at, now),
                )
                timings.append(final_timing)
            else:
                timings.append(timing)
        if final_timing is None:
            raise CommitContractError("Gerber preparation has no running timing")

        state.status = "completed" if succeeded else "failed"
        state.preparation = preparation
        state.error_ids = [error.error_id for error in errors]
        merged = list(session.summary.error_summary)
        known = {error.error_id for error in merged}
        for error in errors:
            if error.error_id not in known:
                merged.append(error)
                known.add(error.error_id)
        session.ops.persist_summary(
            context,
            _validated_summary(
                session.summary,
                phase_timings=timings,
                error_summary=merged,
            ),
        )
        session.ops.append_trajectory(
            context,
            TrajectoryEvent(
                schema_version="1.0",
                run_id=session.run_id,
                timestamp=now,
                phase=PhaseCode.GERBER_PREPARATION,
                branch=BranchCode.GERBER,
                event_type=(
                    TrajectoryEventType.PHASE_FINISHED
                    if succeeded
                    else TrajectoryEventType.PHASE_FAILED
                ),
                summary={"job_material_id": job_material_id, "status": state.status},
                duration_ms=final_timing.duration_ms,
                source_material_ids=[job_material_id],
                artifact_paths=(
                    [self._preparation_artifact_path(job_material_id)] if succeeded else []
                ),
                error_ids=[error.error_id for error in errors],
            ),
        )
        self._check_gerber_readiness(context, session)

    def _execute_gerber_preparation(
        self,
        session,
        job_material_id: str,
        task_key: str,
    ) -> None:
        try:
            session.control.stop_signal.check_boundary()
            if not self._begin_gerber_preparation(session, job_material_id):
                return
            if session.snapshot is None or self.gerber_analyzer is None:
                failure = RuntimeError("Gerber analyzer is unavailable")
                output = None
            else:
                facades = self._analysis_facades(session, branch=BranchCode.GERBER)
                try:
                    output = self.gerber_analyzer.prepare(
                        session.snapshot, job_material_id, facades
                    )
                    failure = None
                except RunStopRequested:
                    return
                except Exception as exc:
                    output = None
                    failure = exc

            if session.control.is_requested():
                return

            def complete(context: RunCommitContext) -> None:
                if (
                    session.summary.status != RunStatus.RUNNING
                    or session.summary.stop_requested_at is not None
                    or not session.control.accepts_business()
                ):
                    return
                if failure is not None:
                    preparation = None
                    errors = self._resolve_preparation_exception(
                        context, session, job_material_id, failure
                    )
                else:
                    try:
                        preparation, returned_errors = output
                    except (TypeError, ValueError):
                        preparation, returned_errors = None, []
                    preparation, errors = self._canonical_preparation_output(
                        context,
                        session,
                        job_material_id,
                        preparation,
                        returned_errors,
                    )
                self._finish_gerber_preparation(
                    context,
                    session,
                    job_material_id,
                    preparation,
                    errors,
                )

            session.channel.execute(complete)
        finally:
            session.control.release_task(task_key)
            session.control.wake()
