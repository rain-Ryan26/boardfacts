from __future__ import annotations

from flyboard.errors import ServiceError
from flyboard.models import (
    AccessLevel,
    ArtifactConsumer,
    ArtifactIndex,
    ArtifactIndexEntry,
    BranchCode,
    BranchResult,
    BranchResultStatus,
    BranchStatus,
    PhaseCode,
    PhaseTiming,
    PhaseTimingStatus,
    RunStatus,
    ScopeType,
    Severity,
    ToolFinding,
    ToolFindingsArtifact,
    TrajectoryEvent,
    TrajectoryEventType,
)

from ._state import (
    BRANCH_ORDER,
    BRANCH_TO_ATTR,
    RESULT_TO_BRANCH,
    branch_sources,
    duration_ms,
    replace_branch_status,
)
from .commit import CommitContractError, RunCommitContext
from .control import RunStopRequested
from .operations import _validated_summary
from .session import RunExecutionSession


class BranchExecutionMixin:
    def _activate_branch(self, session, branch, worker) -> None:
        sources = branch_sources(session.snapshot, branch) if session.snapshot else []
        task_key = f"branch:{branch.value}"

        def command(context: RunCommitContext) -> None:
            if (
                session.summary.status != RunStatus.RUNNING
                or session.summary.stop_requested_at is not None
                or not session.control.accepts_business()
            ):
                return
            now = self.clock()
            timing = PhaseTiming(
                phase=PhaseCode.BRANCH_ANALYSIS,
                scope_type=ScopeType.BRANCH,
                scope_id=branch.value,
                status=PhaseTimingStatus.RUNNING,
                started_at=now,
                finished_at=None,
                duration_ms=None,
            )
            next_summary = _validated_summary(
                session.summary,
                phase_timings=[*session.summary.phase_timings, timing],
                branch_status=replace_branch_status(
                    session.summary.branch_status, branch, BranchStatus.RUNNING
                ),
            )
            session.ops.persist_summary(context, next_summary)
            session.ops.append_trajectory(
                context,
                TrajectoryEvent(
                    schema_version="1.0",
                    run_id=session.run_id,
                    timestamp=now,
                    phase=PhaseCode.BRANCH_ANALYSIS,
                    branch=branch,
                    event_type=TrajectoryEventType.PHASE_STARTED,
                    summary={"branch": branch.value},
                    source_material_ids=sources,
                    artifact_paths=[],
                    error_ids=[],
                ),
            )
            try:
                submitted = session.ops.submit_worker(
                    context,
                    self.executor,
                    task_key=task_key,
                    worker=lambda: self._execute_branch(session, branch, sources, worker, task_key),
                )
            except Exception:
                error = session.ops.record_error(
                    context,
                    code="run_dispatch_failed",
                    severity=Severity.ERROR,
                    phase=PhaseCode.BRANCH_ANALYSIS,
                    branch=branch,
                    message="分支 worker 未能提交到本地执行器。",
                    source_material_ids=sources,
                    artifact_paths=[],
                    suggested_action="检查本地执行资源后创建新的运行。",
                )
                self._commit_branch_result(
                    context,
                    session,
                    self._failed_branch_result(session.run_id, branch, sources, error),
                    sources,
                )
                return
            if not submitted:
                return

        session.channel.execute(command)

    @staticmethod
    def _failed_branch_result(run_id, branch, sources, error) -> BranchResult:
        return BranchResult(
            schema_version="1.0",
            run_id=run_id,
            branch=branch,
            status=BranchResultStatus.FAILED,
            source_material_ids=sources,
            fields=[],
            unknown_notes=[],
            tool_findings=[],
            evidence=[],
            errors=[error],
        )

    def _canonical_branch_result(self, context, session, branch, sources, result) -> BranchResult:
        try:
            if not isinstance(result, BranchResult):
                raise ValueError("worker did not return BranchResult")
            if (
                result.run_id != session.run_id
                or result.branch != branch
                or result.source_material_ids != sources
            ):
                raise ValueError("branch result identity or source order mismatch")
            if branch == BranchCode.GERBER:
                result = self._merge_gerber_preparation_errors(context, session, result)
            for error in result.errors:
                session.ops.validate_error_identity(context, error)
            for finding in result.tool_findings:
                session.ops.validate_finding_identity(context, finding)
                if finding.error_id is not None:
                    session.ops.resolve_error(context, finding.error_id)
            return result
        except (ValueError, CommitContractError):
            error = session.ops.record_error(
                context,
                code="branch_result_invalid",
                severity=Severity.ERROR,
                phase=PhaseCode.BRANCH_ANALYSIS,
                branch=branch,
                message="分支返回结果未通过运行合同校验。",
                source_material_ids=sources,
                artifact_paths=[],
                suggested_action="检查分支实现和诊断身份复用后创建新的运行。",
            )
            return self._failed_branch_result(session.run_id, branch, sources, error)

    def _commit_branch_result(
        self,
        context,
        session,
        result: BranchResult,
        sources: list[str],
        *,
        require_running_timing: bool = True,
    ) -> None:
        branch = result.branch
        entry = ArtifactIndexEntry(
            path=f"branches/{branch.value}_result.json",
            kind="branch_result",
            producer_phase=PhaseCode.BRANCH_ANALYSIS,
            consumers=[
                ArtifactConsumer.WORKFLOW,
                ArtifactConsumer.DEBUG,
                ArtifactConsumer.REGRESSION,
            ],
            access_level=AccessLevel.RESTRICTED_DEBUG,
            source_material_ids=sources,
        )
        try:
            self.artifacts.replace_json_artifact(
                session.project_id,
                session.run_id,
                artifact=result,
                entry=entry,
            )
        except ServiceError as exc:
            error = session.ops.record_error(
                context,
                code="branch_result_write_failed",
                severity=Severity.ERROR,
                phase=PhaseCode.BRANCH_ANALYSIS,
                branch=branch,
                message="分支终态结果未能完成原子写入。",
                source_material_ids=sources,
                artifact_paths=[],
                suggested_action="检查受管存储后创建新的运行。",
            )
            try:
                session.ops.add_errors_to_summary(context, [error])
            except ServiceError:
                pass
            session.control.close_business()
            raise ServiceError("branch_result_write_failed", error_id=error.error_id) from exc

        session.branch_results[branch] = result
        if session.artifact_index_available:
            entries = [item for item in session.artifact_index.entries if item.path != entry.path]
            next_index = ArtifactIndex(
                schema_version="1.0",
                run_id=session.run_id,
                entries=[*entries, entry],
            )
            try:
                self.artifacts.replace_artifact_index(
                    session.project_id, session.run_id, next_index
                )
            except ServiceError:
                session.ops._record_index_failure(context, entry=entry, branch=branch)
            else:
                session.artifact_index = next_index

        now = self.clock()
        timings: list[PhaseTiming] = []
        final_timing: PhaseTiming | None = None
        for timing in session.summary.phase_timings:
            if (
                timing.phase == PhaseCode.BRANCH_ANALYSIS
                and timing.scope_type == ScopeType.BRANCH
                and timing.scope_id == branch.value
                and timing.status == PhaseTimingStatus.RUNNING
            ):
                timing_status = (
                    PhaseTimingStatus.FAILED
                    if result.status == BranchResultStatus.FAILED
                    else (
                        PhaseTimingStatus.COMPLETED_WITH_ERRORS
                        if any(item.severity == Severity.ERROR for item in result.errors)
                        else PhaseTimingStatus.COMPLETED
                    )
                )
                final_timing = PhaseTiming(
                    phase=timing.phase,
                    scope_type=timing.scope_type,
                    scope_id=timing.scope_id,
                    status=timing_status,
                    started_at=timing.started_at,
                    finished_at=now,
                    duration_ms=duration_ms(timing.started_at, now),
                )
                timings.append(final_timing)
            else:
                timings.append(timing)
        if final_timing is None and require_running_timing:
            raise CommitContractError("branch completion has no running timing")
        merged = list(session.summary.error_summary)
        known = {item.error_id for item in merged}
        for error in result.errors:
            if error.error_id not in known:
                merged.append(error)
                known.add(error.error_id)
        next_summary = _validated_summary(
            session.summary,
            phase_timings=timings,
            branch_status=replace_branch_status(
                session.summary.branch_status, branch, RESULT_TO_BRANCH[result.status]
            ),
            error_summary=merged,
        )
        session.ops.persist_summary(context, next_summary)
        session.ops.append_trajectory(
            context,
            TrajectoryEvent(
                schema_version="1.0",
                run_id=session.run_id,
                timestamp=now,
                phase=PhaseCode.BRANCH_ANALYSIS,
                branch=branch,
                event_type=(
                    TrajectoryEventType.PHASE_FAILED
                    if result.status == BranchResultStatus.FAILED
                    else TrajectoryEventType.PHASE_FINISHED
                ),
                summary={"branch": branch.value, "status": result.status.value},
                duration_ms=final_timing.duration_ms if final_timing is not None else None,
                source_material_ids=sources,
                artifact_paths=[entry.path],
                error_ids=[item.error_id for item in result.errors],
            ),
        )

    def _execute_branch(self, session, branch, sources, worker, task_key) -> None:
        if session.snapshot is None:
            session.control.release_task(task_key)
            return
        facades = self._analysis_facades(session, branch=branch)
        unhandled = False
        try:
            result = worker(session.snapshot, facades)
        except RunStopRequested:
            result = None
        except Exception:
            result = None
            unhandled = True
        try:
            if session.control.is_requested():
                return

            def commit(context: RunCommitContext) -> None:
                if (
                    session.summary.status != RunStatus.RUNNING
                    or session.summary.stop_requested_at is not None
                    or not session.control.accepts_business()
                ):
                    return
                candidate = result
                if unhandled:
                    error = session.ops.record_error(
                        context,
                        code="branch_unhandled_exception",
                        severity=Severity.ERROR,
                        phase=PhaseCode.BRANCH_ANALYSIS,
                        branch=branch,
                        message="分支执行发生未处理异常。",
                        source_material_ids=sources,
                        artifact_paths=[],
                        suggested_action="检查分支日志后创建新的运行。",
                    )
                    candidate = self._failed_branch_result(session.run_id, branch, sources, error)
                if candidate is None:
                    return
                canonical = self._canonical_branch_result(
                    context, session, branch, sources, candidate
                )
                self._commit_branch_result(context, session, canonical, sources)

            session.channel.execute(commit)
        except Exception as exc:
            if branch in session.branch_results:
                session.control.wake()
                return
            if not session.product_terminal_committed:
                try:
                    self._fail_session(
                        session,
                        code="branch_result_write_failed",
                        phase=PhaseCode.BRANCH_ANALYSIS,
                        branch=branch,
                        message="分支终态提交未能完整收口。",
                        source_material_ids=sources,
                        suggested_action="检查受管存储后创建新的运行。",
                        existing_error_id=(
                            exc.error_id
                            if isinstance(exc, ServiceError) and exc.error_id is not None
                            else None
                        ),
                    )
                except Exception:
                    session.control.close_business()
        finally:
            session.control.release_task(task_key)

    def _all_branches_terminal(self, session: RunExecutionSession) -> bool:
        if session.snapshot is None:
            return False
        for branch in BRANCH_ORDER:
            sources = branch_sources(session.snapshot, branch)
            status = getattr(session.summary.branch_status, BRANCH_TO_ATTR[branch])
            if not sources:
                if status != BranchStatus.SKIPPED:
                    return False
            elif branch not in session.branch_results or status not in {
                BranchStatus.COMPLETED,
                BranchStatus.COMPLETED_WITH_WARNINGS,
                BranchStatus.FAILED,
            }:
                return False
        return True

    def _aggregate_tool_findings(self, session: RunExecutionSession) -> None:
        def command(context: RunCommitContext) -> None:
            if session.tool_findings is not None:
                return
            findings: list[ToolFinding] = []
            for branch in BRANCH_ORDER:
                result = session.branch_results.get(branch)
                if result is None:
                    continue
                for finding in result.tool_findings:
                    session.ops.validate_finding_identity(context, finding)
                    findings.append(finding)
            session.tool_findings = findings
            artifact = ToolFindingsArtifact(
                schema_version="1.0",
                run_id=session.run_id,
                tool_findings=findings,
            )
            entry = ArtifactIndexEntry(
                path="integration/tool_findings.json",
                kind="tool_findings",
                producer_phase=PhaseCode.BRANCH_ANALYSIS,
                consumers=[
                    ArtifactConsumer.API,
                    ArtifactConsumer.GUI,
                    ArtifactConsumer.REPORT,
                    ArtifactConsumer.DEBUG,
                    ArtifactConsumer.REGRESSION,
                ],
                access_level=AccessLevel.PROJECT_SCOPED,
                source_material_ids=[],
            )
            try:
                session.ops.write_json(
                    context,
                    artifact,
                    entry,
                    target_error_code="tool_findings_write_failed",
                )
            except ServiceError as exc:
                if exc.code == "artifact_index_update_failed":
                    return
                if exc.code == "tool_findings_write_failed":
                    error = session.ops.resolve_error(context, exc.error_id)
                    session.ops.add_errors_to_summary(context, [error])
                    return
                raise
            session.tool_findings_committed = True

        session.channel.execute(command)

    @staticmethod
    def _has_adoptable_branch(session: RunExecutionSession) -> bool:
        return any(
            result.status
            in {
                BranchResultStatus.COMPLETED,
                BranchResultStatus.COMPLETED_WITH_WARNINGS,
            }
            and bool(result.fields or result.unknown_notes)
            for result in session.branch_results.values()
        )
