from __future__ import annotations

import logging

from flyboard.errors import ServiceError
from flyboard.models import (
    BranchCode,
    BranchStatus,
    BranchStatusMap,
    PhaseCode,
    RunStatus,
    Severity,
    TrajectoryEvent,
    TrajectoryEventType,
)

from ._state import (
    BRANCH_ORDER,
    branch_sources,
    skipped_branch_timing,
    terminal_summary,
)
from .commit import RunCommitContext
from .operations import _validated_summary

_LOGGER = logging.getLogger(__name__)


class CoordinatorMixin:
    def _coordinator_start(self, session) -> bool:
        def command(context: RunCommitContext) -> bool:
            if session.summary.status != RunStatus.QUEUED:
                return False
            if session.control.is_requested() or not session.control.accepts_business():
                return False
            now = self.clock()
            timings = list(session.summary.phase_timings)
            for branch in BRANCH_ORDER:
                sources = branch_sources(session.snapshot, branch) if session.snapshot else []
                if not sources:
                    timings.append(skipped_branch_timing(branch, now))
            statuses = BranchStatusMap(
                pdf_image=(
                    BranchStatus.QUEUED
                    if session.snapshot and branch_sources(session.snapshot, BRANCH_ORDER[0])
                    else BranchStatus.SKIPPED
                ),
                xlsx=(
                    BranchStatus.QUEUED
                    if session.snapshot and branch_sources(session.snapshot, BRANCH_ORDER[1])
                    else BranchStatus.SKIPPED
                ),
                gerber=(
                    BranchStatus.QUEUED
                    if session.snapshot and branch_sources(session.snapshot, BRANCH_ORDER[2])
                    else BranchStatus.SKIPPED
                ),
            )
            session.ops.persist_summary(
                context,
                _validated_summary(
                    session.summary,
                    status=RunStatus.RUNNING,
                    current_phase=PhaseCode.BRANCH_ANALYSIS,
                    phase_timings=timings,
                    branch_status=statuses,
                ),
            )
            return True

        return session.channel.execute(command)

    def _coordinator_main(self, session) -> None:
        try:
            if not self._coordinator_start(session):
                return
            if session.snapshot is None:
                self._fail_session(
                    session,
                    code="service_interrupted",
                    phase=None,
                    branch=None,
                    message="活动运行缺少冻结输入快照。",
                    source_material_ids=[],
                    suggested_action="创建新的运行。",
                    service_interrupted=True,
                )
                return
            if not any(branch_sources(session.snapshot, branch) for branch in BRANCH_ORDER):
                self._fail_session(
                    session,
                    code="no_processable_materials",
                    phase=PhaseCode.BRANCH_ANALYSIS,
                    branch=None,
                    message="本次选择中没有可进入分析分支的资料。",
                    source_material_ids=[item.material_id for item in session.snapshot.materials],
                    suggested_action="选择至少一项受支持的资料后创建新的运行。",
                )
                return

            missing_owner = next(
                (
                    branch
                    for branch in BRANCH_ORDER
                    if branch_sources(session.snapshot, branch)
                    and (
                        self.gerber_analyzer is None
                        if branch == BranchCode.GERBER
                        else self.branch_workers.get(branch) is None
                    )
                ),
                None,
            )
            if missing_owner is not None:
                self._fail_session(
                    session,
                    code="run_dispatch_failed",
                    phase=PhaseCode.BRANCH_ANALYSIS,
                    branch=missing_owner,
                    message="所选资料分支尚未装配唯一执行 owner。",
                    source_material_ids=branch_sources(session.snapshot, missing_owner),
                    suggested_action="完成对应分析分支装配后创建新的运行。",
                )
                return

            if session.snapshot.groups.gerber_jobs:
                self._activate_gerber_preparations(session)

            for branch in BRANCH_ORDER:
                sources = branch_sources(session.snapshot, branch)
                worker = self.branch_workers.get(branch)
                if branch != BranchCode.GERBER and sources and worker is not None:
                    self._activate_branch(session, branch, worker)

            while not session.product_terminal_committed:
                reason = session.control.reason
                if reason == "user_requested" and session.control.task_count() == 0:
                    self._finalize_stop(session)
                    return
                if reason == "service_shutdown" and session.control.task_count() == 0:
                    self._fail_session(
                        session,
                        code="service_interrupted",
                        phase=session.summary.current_phase,
                        branch=None,
                        message="服务关闭中断了尚未完成的运行。",
                        source_material_ids=[],
                        suggested_action="服务恢复后创建新的运行。",
                        service_interrupted=True,
                    )
                    return
                if (
                    not session.control.accepts_business()
                    and reason is None
                    and session.control.task_count() == 0
                ):
                    existing_error = next(
                        (
                            error
                            for error in reversed(session.summary.error_summary)
                            if error.code
                            in {"run_record_write_failed", "branch_result_write_failed"}
                        ),
                        None,
                    )
                    self._fail_session(
                        session,
                        code=(
                            existing_error.code
                            if existing_error is not None
                            else "run_record_write_failed"
                        ),
                        phase=session.summary.current_phase,
                        branch=None,
                        message="运行根记录失败，后续业务提交已经关闭。",
                        source_material_ids=[],
                        suggested_action="检查受管存储后创建新的运行。",
                        existing_error_id=(existing_error.error_id if existing_error else None),
                    )
                    return
                if self._all_branches_terminal(session):
                    self._aggregate_tool_findings(session)
                    if not self._has_adoptable_branch(session):
                        self._fail_session(
                            session,
                            code="no_adoptable_branch_results",
                            phase=PhaseCode.BRANCH_ANALYSIS,
                            branch=None,
                            message="所有可达分析分支均未形成可采纳业务结果。",
                            source_material_ids=[],
                            suggested_action="查看分支错误摘要并修正输入后重新运行。",
                        )
                        return
                    self._run_result_pipeline(session)
                    if not session.product_terminal_committed:
                        reason = session.control.reason
                        if reason == "user_requested":
                            self._finalize_stop(session)
                        elif reason == "service_shutdown":
                            self._fail_session(
                                session,
                                code="service_interrupted",
                                phase=session.summary.current_phase,
                                branch=None,
                                message="服务关闭中断了尚未完成的运行。",
                                source_material_ids=[],
                                suggested_action="服务恢复后创建新的运行。",
                                service_interrupted=True,
                            )
                        else:
                            self._fail_session(
                                session,
                                code="run_dispatch_failed",
                                phase=session.summary.current_phase,
                                branch=None,
                                message="下游结果链未能完成终态收口。",
                                source_material_ids=[],
                                suggested_action="检查下游 owner 后创建新的运行。",
                            )
                    return

                session.control.wait(1.0)
        except Exception as exc:
            _LOGGER.exception("run coordinator failed for %s", session.run_id)
            if not session.product_terminal_committed:
                try:
                    self._fail_session(
                        session,
                        code="run_dispatch_failed",
                        phase=session.summary.current_phase,
                        branch=None,
                        message="运行协调器发生未处理异常。",
                        source_material_ids=[],
                        suggested_action="检查运行日志后创建新的运行。",
                        existing_error_id=(
                            exc.error_id
                            if isinstance(exc, ServiceError) and exc.error_id is not None
                            else None
                        ),
                    )
                except Exception:
                    _LOGGER.exception("run coordinator could not persist terminal failure")
                    session.control.close_business()
        finally:
            if session.product_terminal_committed:
                self._run_terminal_tail(session)
            with self._coordinator_guard:
                self._coordinators.pop((session.project_id, session.run_id), None)

    def _fail_session(
        self,
        session,
        *,
        code,
        phase,
        branch,
        message,
        source_material_ids,
        suggested_action,
        service_interrupted=False,
        existing_error_id=None,
    ) -> None:
        def command(context: RunCommitContext) -> None:
            if session.summary.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
                return
            error = (
                session.ops.resolve_error(context, existing_error_id)
                if existing_error_id is not None
                else session.ops.record_error(
                    context,
                    code=code,
                    severity=Severity.ERROR,
                    phase=phase,
                    branch=branch,
                    message=message,
                    source_material_ids=source_material_ids,
                    artifact_paths=[],
                    suggested_action=suggested_action,
                )
            )
            now = self.clock()
            session.ops.persist_summary(
                context,
                terminal_summary(
                    session.summary,
                    status=RunStatus.FAILED,
                    finished_at=now,
                    errors=[error],
                    service_interrupted=service_interrupted,
                ),
            )
            session.product_terminal_committed = True
            session.control.close_business()
            if error.code == "run_record_write_failed":
                return
            try:
                session.ops.append_trajectory(
                    context,
                    TrajectoryEvent(
                        schema_version="1.0",
                        run_id=session.run_id,
                        timestamp=now,
                        phase=phase,
                        branch=branch,
                        event_type=TrajectoryEventType.RUN_FAILED,
                        summary={"code": error.code},
                        source_material_ids=source_material_ids,
                        artifact_paths=[],
                        error_ids=[error.error_id],
                    ),
                )
            except ServiceError:
                pass

        session.channel.execute(command)

    def _finalize_stop(self, session) -> None:
        def command(context: RunCommitContext) -> None:
            if session.summary.status == RunStatus.STOPPED:
                return
            if session.summary.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
                return
            now = self.clock()
            session.ops.persist_summary(
                context,
                terminal_summary(
                    session.summary,
                    status=RunStatus.STOPPED,
                    finished_at=now,
                    stop_requested_at=session.summary.stop_requested_at or now,
                ),
            )
            session.product_terminal_committed = True
            session.control.close_business()
            try:
                session.ops.append_trajectory(
                    context,
                    TrajectoryEvent(
                        schema_version="1.0",
                        run_id=session.run_id,
                        timestamp=now,
                        phase=session.summary.current_phase,
                        branch=None,
                        event_type=TrajectoryEventType.RUN_STOPPED,
                        summary={"reason": "user_requested"},
                        source_material_ids=[],
                        artifact_paths=[],
                        error_ids=[],
                    ),
                )
            except ServiceError:
                pass

        session.channel.execute(command)
