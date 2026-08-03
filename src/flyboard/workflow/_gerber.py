from __future__ import annotations

from flyboard.errors import ServiceError
from flyboard.models import (
    BranchCode,
    BranchResult,
    BranchResultStatus,
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

from ._gerber_preparation import GerberPreparationMixin
from ._state import branch_sources, replace_branch_status
from .commit import CommitContractError, RunCommitContext
from .operations import _validated_summary


class GerberCoordinationMixin(GerberPreparationMixin):
    def _gerber_errors(self, context: RunCommitContext, session) -> list[ErrorRecord]:
        if session.snapshot is None:
            return []
        errors: list[ErrorRecord] = []
        seen: set[str] = set()
        for job_material_id in session.snapshot.groups.gerber_jobs:
            state = session.preparation_states_by_job[job_material_id]
            for error_id in state.error_ids:
                if error_id not in seen:
                    errors.append(session.ops.resolve_error(context, error_id))
                    seen.add(error_id)
        return errors

    def _merge_gerber_preparation_errors(
        self,
        context: RunCommitContext,
        session,
        result: BranchResult,
    ) -> BranchResult:
        preparation_errors = self._gerber_errors(context, session)
        merged = list(preparation_errors)
        known = {error.error_id for error in merged}
        for error in result.errors:
            if error.error_id not in known:
                merged.append(error)
                known.add(error.error_id)
        status = result.status
        if status == BranchResultStatus.COMPLETED and merged:
            status = BranchResultStatus.COMPLETED_WITH_WARNINGS
        return result.model_copy(update={"status": status, "errors": merged})

    def _fail_gerber_readiness(
        self,
        context: RunCommitContext,
        session,
        failure: Exception,
    ) -> None:
        try:
            if (
                isinstance(failure, ServiceError)
                and failure.code == "run_record_write_failed"
                and failure.error_id is not None
            ):
                session.ops.resolve_error(context, failure.error_id)
                return
            if (
                session.snapshot is None
                or BranchCode.GERBER in session.branch_results
                or session.summary.status != RunStatus.RUNNING
                or not session.control.accepts_business()
            ):
                return
            error = session.ops.record_error(
                context,
                code="gerber_readiness_failed",
                severity=Severity.ERROR,
                phase=PhaseCode.BRANCH_ANALYSIS,
                branch=BranchCode.GERBER,
                message="Gerber readiness 未能完成一致性检查或调度。",
                source_material_ids=list(session.snapshot.groups.gerber_jobs),
                artifact_paths=[],
                suggested_action="查看运行错误摘要后创建新的运行。",
            )
            session.gerber_analysis_scheduled = True
            errors = self._gerber_errors(context, session)
            if all(item.error_id != error.error_id for item in errors):
                errors.append(error)
            sources = branch_sources(session.snapshot, BranchCode.GERBER)
            has_running_timing = any(
                timing.phase == PhaseCode.BRANCH_ANALYSIS
                and timing.scope_type == ScopeType.BRANCH
                and timing.scope_id == BranchCode.GERBER.value
                and timing.status == PhaseTimingStatus.RUNNING
                for timing in session.summary.phase_timings
            )
            self._commit_branch_result(
                context,
                session,
                BranchResult(
                    schema_version="1.0",
                    run_id=session.run_id,
                    branch=BranchCode.GERBER,
                    status=BranchResultStatus.FAILED,
                    source_material_ids=sources,
                    fields=[],
                    unknown_notes=[],
                    tool_findings=[],
                    evidence=[],
                    errors=errors,
                ),
                sources,
                require_running_timing=has_running_timing,
            )
        except Exception:
            session.control.close_business()

    def _begin_gerber_branch_timing(
        self,
        context: RunCommitContext,
        session,
    ) -> list[str]:
        if session.snapshot is None:
            raise CommitContractError("Gerber branch timing requires a snapshot")
        sources = branch_sources(session.snapshot, BranchCode.GERBER)
        now = self.clock()
        if any(
            timing.phase == PhaseCode.BRANCH_ANALYSIS
            and timing.scope_type == ScopeType.BRANCH
            and timing.scope_id == BranchCode.GERBER.value
            for timing in session.summary.phase_timings
        ):
            raise CommitContractError("duplicate Gerber analysis timing")
        timing = PhaseTiming(
            phase=PhaseCode.BRANCH_ANALYSIS,
            scope_type=ScopeType.BRANCH,
            scope_id=BranchCode.GERBER.value,
            status=PhaseTimingStatus.RUNNING,
            started_at=now,
            finished_at=None,
            duration_ms=None,
        )
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
                phase=PhaseCode.BRANCH_ANALYSIS,
                branch=BranchCode.GERBER,
                event_type=TrajectoryEventType.PHASE_STARTED,
                summary={"branch": BranchCode.GERBER.value},
                source_material_ids=sources,
                artifact_paths=[],
                error_ids=[],
            ),
        )
        return sources

    def _start_gerber_analysis(
        self,
        context: RunCommitContext,
        session,
        preparations: list[GerberJobPreparation],
        definitions,
    ) -> None:
        if session.snapshot is None or self.gerber_analyzer is None:
            raise CommitContractError("Gerber analysis owner is unavailable")
        sources = self._begin_gerber_branch_timing(context, session)
        task_key = "branch:gerber"

        def worker(snapshot, facades):
            return self.gerber_analyzer.analyze(
                snapshot,
                facades,
                preparations,
                definitions,
            )

        try:
            submitted = session.ops.submit_worker(
                context,
                self.executor,
                task_key=task_key,
                worker=lambda: self._execute_branch(
                    session,
                    BranchCode.GERBER,
                    sources,
                    worker,
                    task_key,
                ),
            )
        except Exception:
            error = session.ops.record_error(
                context,
                code="run_dispatch_failed",
                severity=Severity.ERROR,
                phase=PhaseCode.BRANCH_ANALYSIS,
                branch=BranchCode.GERBER,
                message="Gerber 完整分析 worker 未能提交到本地执行器。",
                source_material_ids=sources,
                artifact_paths=[],
                suggested_action="检查本地执行资源后创建新的运行。",
            )
            self._commit_branch_result(
                context,
                session,
                self._failed_branch_result(
                    session.run_id,
                    BranchCode.GERBER,
                    sources,
                    error,
                ),
                sources,
            )
            return
        if not submitted:
            return

    def _check_gerber_readiness(
        self,
        context: RunCommitContext,
        session,
    ) -> None:
        if session.snapshot is None:
            raise CommitContractError("Gerber readiness requires a snapshot")
        if (
            session.summary.status != RunStatus.RUNNING
            or session.summary.stop_requested_at is not None
            or session.summary.branch_status.gerber
            in {
                BranchStatus.STOPPED,
                BranchStatus.SKIPPED,
                BranchStatus.COMPLETED,
                BranchStatus.COMPLETED_WITH_WARNINGS,
                BranchStatus.FAILED,
            }
        ):
            return
        ordered_states = [
            session.preparation_states_by_job[job_id]
            for job_id in session.snapshot.groups.gerber_jobs
        ]
        if any(state.status in {"queued", "running"} for state in ordered_states):
            return
        completed = [
            state
            for state in ordered_states
            if state.status == "completed" and isinstance(state.preparation, GerberJobPreparation)
        ]
        sources = branch_sources(session.snapshot, BranchCode.GERBER)
        if not completed:
            errors = self._gerber_errors(context, session)
            if not errors:
                errors = [
                    session.ops.record_error(
                        context,
                        code="gerber_input_parse_failed",
                        severity=Severity.ERROR,
                        phase=PhaseCode.GERBER_PREPARATION,
                        branch=BranchCode.GERBER,
                        message="全部 Gerber job preparation 均未形成可确认结果。",
                        source_material_ids=sources,
                        artifact_paths=[],
                        suggested_action="检查 Gerber job 内容后创建新的运行。",
                    )
                ]
            self._begin_gerber_branch_timing(context, session)
            result = BranchResult(
                schema_version="1.0",
                run_id=session.run_id,
                branch=BranchCode.GERBER,
                status=BranchResultStatus.FAILED,
                source_material_ids=sources,
                fields=[],
                unknown_notes=[],
                tool_findings=[],
                evidence=[],
                errors=errors,
            )
            self._commit_branch_result(context, session, result, sources)
            return
        definitions = []
        preparations = []
        for job_material_id in session.snapshot.groups.gerber_jobs:
            state = session.preparation_states_by_job[job_material_id]
            if state.status != "completed":
                continue
            preparation = state.preparation
            definition = session.definitions_by_job.get(job_material_id)
            if (
                not isinstance(preparation, GerberJobPreparation)
                or definition is None
                or definition.confirmed_at is None
            ):
                return
            preparations.append(preparation)
            definitions.append(definition)
        if session.gerber_analysis_scheduled:
            return
        session.gerber_analysis_scheduled = True
        self._start_gerber_analysis(context, session, preparations, definitions)
