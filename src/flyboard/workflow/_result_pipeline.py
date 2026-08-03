from __future__ import annotations

from flyboard.models import (
    EvidenceIndex,
    IntegrationResult,
    PhaseCode,
    PhaseTimingStatus,
    RuleCheckResult,
    RuleCheckResultStatus,
    RuleCheckStatus,
    RuleCheckSummary,
    RunStatus,
    Severity,
)

from ._pipeline_phases import PipelinePhaseMixin
from ._state import AnalysisContext, terminal_summary
from .commit import CommitContractError, RunCommitContext
from .control import RunStopRequested


class ResultPipelineMixin(PipelinePhaseMixin):
    def _run_integration(self, session, context: AnalysisContext) -> bool:
        if self.integrator is None:
            if not self._start_phase(session, PhaseCode.INTEGRATION):
                return False
            error = self._record_stage_error(
                session,
                phase=PhaseCode.INTEGRATION,
                code="run_dispatch_failed",
                message="整合阶段尚未装配唯一执行 owner。",
            )
            self._fail_started_stage(session, PhaseCode.INTEGRATION, error)
            return False
        if not self._start_phase(session, PhaseCode.INTEGRATION):
            return False
        branch_results = self._ordered_branch_results(session)
        try:
            result = self.integrator.merge(
                session.snapshot,
                branch_results,
                session.tool_findings,
                context,
            )
        except RunStopRequested:
            return False
        except Exception:
            result = None
        draft_indexed = any(
            entry.kind == "draft_project_data_list"
            and entry.path == "integration/draft_project_data_list.json"
            for entry in session.artifact_index.entries
        )
        if (
            not isinstance(result, IntegrationResult)
            or not result.draft_project_data_list
            or result.tool_findings != session.tool_findings
            or (session.artifact_index_available and not draft_indexed)
        ):
            error = self._record_stage_error(
                session,
                phase=PhaseCode.INTEGRATION,
                code="integration_draft_invalid",
                message="整合阶段未形成通过合同校验的规则前草稿。",
            )
            self._fail_started_stage(session, PhaseCode.INTEGRATION, error)
            return False
        try:
            session.channel.execute(
                lambda commit_context: [
                    session.ops.validate_error_identity(commit_context, error)
                    for error in result.errors
                ]
            )
        except CommitContractError:
            error = self._record_stage_error(
                session,
                phase=PhaseCode.INTEGRATION,
                code="integration_draft_invalid",
                message="整合阶段返回了不属于本次 run 的诊断身份。",
            )
            self._fail_started_stage(session, PhaseCode.INTEGRATION, error)
            return False
        session.integration_result = result
        session.integration_committed = True
        errors = self._stage_errors(session, PhaseCode.INTEGRATION)
        self._finish_phase(
            session,
            PhaseCode.INTEGRATION,
            timing_status=(
                PhaseTimingStatus.COMPLETED_WITH_ERRORS
                if any(item.severity == Severity.ERROR for item in errors)
                else PhaseTimingStatus.COMPLETED
            ),
            errors=errors,
            event_summary={"row_count": len(result.draft_project_data_list)},
        )
        return True

    def _rule_summary_from_result(self, result: RuleCheckResult) -> RuleCheckSummary:
        if result.status == RuleCheckResultStatus.SKIPPED:
            return RuleCheckSummary(
                status=RuleCheckStatus.SKIPPED,
                rule_set_id=None,
                rule_set_version=None,
                attempted_rule_count=0,
                error_count=0,
                reason="rule_set_not_selected",
            )
        status = {
            RuleCheckResultStatus.COMPLETED: RuleCheckStatus.COMPLETED,
            RuleCheckResultStatus.COMPLETED_WITH_ERRORS: RuleCheckStatus.COMPLETED_WITH_ERRORS,
            RuleCheckResultStatus.INTERRUPTED: RuleCheckStatus.INTERRUPTED,
        }[result.status]
        return RuleCheckSummary(
            status=status,
            rule_set_id=result.rule_set.rule_set_id,
            rule_set_version=result.rule_set.rule_set_version,
            attempted_rule_count=result.attempted_rule_count,
            error_count=(
                None
                if result.status == RuleCheckResultStatus.INTERRUPTED
                else len({item.error_id for item in result.errors})
            ),
            reason=result.reason,
        )

    def _rule_result_is_valid(self, session, result) -> bool:
        if not isinstance(result, RuleCheckResult) or result.run_id != session.run_id:
            return False
        selection = session.snapshot.rule_set
        if selection.mode == "not_selected":
            return result.status == RuleCheckResultStatus.SKIPPED
        if result.status == RuleCheckResultStatus.SKIPPED or result.rule_set is None:
            return False
        return (
            result.rule_set.rule_set_id == selection.rule_set_id
            and result.rule_set.rule_set_version == selection.rule_set_version
            and result.rule_set.content_sha256 == selection.content_sha256
        )

    def _run_rule_check(self, session, context: AnalysisContext) -> bool:
        selected = session.snapshot.rule_set.mode == "selected"
        if self.rule_engine is None:
            if not self._start_phase(session, PhaseCode.RULE_CHECK, rule_running=selected):
                return False
            error = self._record_stage_error(
                session,
                phase=PhaseCode.RULE_CHECK,
                code="run_dispatch_failed",
                message="规则检查阶段尚未装配唯一执行 owner。",
            )
            self._fail_started_stage(session, PhaseCode.RULE_CHECK, error)
            return False
        entered = (
            self._start_phase(session, PhaseCode.RULE_CHECK, rule_running=True)
            if selected
            else self._enter_rule_skip(session)
        )
        if not entered:
            return False
        try:
            result = self.rule_engine.check(
                session.snapshot,
                session.integration_result,
                session.rule_set_snapshot,
                context,
            )
        except RunStopRequested:
            return False
        except Exception:
            result = None
        if not self._rule_result_is_valid(session, result):
            error = self._record_stage_error(
                session,
                phase=PhaseCode.RULE_CHECK,
                code="rule_check_failed",
                message="规则检查阶段未形成通过合同校验的结果。",
            )
            if selected:
                self._fail_started_stage(session, PhaseCode.RULE_CHECK, error)
            else:
                self._fail_session(
                    session,
                    code=error.code,
                    phase=PhaseCode.RULE_CHECK,
                    branch=None,
                    message=error.message,
                    source_material_ids=[],
                    suggested_action=error.suggested_action,
                    existing_error_id=error.error_id,
                )
            return False
        try:
            session.channel.execute(
                lambda commit_context: [
                    session.ops.validate_error_identity(commit_context, error)
                    for error in result.errors
                ]
            )
        except CommitContractError:
            error = self._record_stage_error(
                session,
                phase=PhaseCode.RULE_CHECK,
                code="rule_check_failed",
                message="规则检查阶段返回了不属于本次 run 的诊断身份。",
            )
            if selected:
                self._fail_started_stage(session, PhaseCode.RULE_CHECK, error)
            else:
                self._fail_session(
                    session,
                    code=error.code,
                    phase=PhaseCode.RULE_CHECK,
                    branch=None,
                    message=error.message,
                    source_material_ids=[],
                    suggested_action=error.suggested_action,
                    existing_error_id=error.error_id,
                )
            return False
        final_rows = session.integration_result.project_data_list
        if final_rows is None or session.summary.project_data_list_path != "project_data_list.json":
            error = self._record_stage_error(
                session,
                phase=PhaseCode.RULE_CHECK,
                code="rule_check_failed",
                message="规则检查阶段没有提交最终项目数据列表。",
            )
            if selected:
                self._fail_started_stage(session, PhaseCode.RULE_CHECK, error)
            else:
                self._fail_session(
                    session,
                    code=error.code,
                    phase=PhaseCode.RULE_CHECK,
                    branch=None,
                    message=error.message,
                    source_material_ids=[],
                    suggested_action=error.suggested_action,
                    existing_error_id=error.error_id,
                )
            return False
        session.rule_check_result = result
        session.rule_check_result_committed = bool(
            session.artifact_index_available
            and any(
                entry.kind == "rule_check_result" and entry.path == "rules/rule_check_result.json"
                for entry in session.artifact_index.entries
            )
        )
        session.final_project_data_list = final_rows
        session.final_project_data_list_committed = True
        rule_summary = self._rule_summary_from_result(result)
        errors = self._stage_errors(session, PhaseCode.RULE_CHECK)
        if selected:
            self._finish_phase(
                session,
                PhaseCode.RULE_CHECK,
                timing_status=(
                    PhaseTimingStatus.COMPLETED_WITH_ERRORS
                    if errors or result.status != RuleCheckResultStatus.COMPLETED
                    else PhaseTimingStatus.COMPLETED
                ),
                errors=errors,
                summary_values={"rule_check_summary": rule_summary},
                event_summary={
                    "status": result.status.value,
                    "attempted_rule_count": result.attempted_rule_count,
                },
            )
        else:
            self._skip_phase(
                session,
                PhaseCode.RULE_CHECK,
                reason="rule_set_not_selected",
                error_ids=[],
            )
        return True

    def _run_evidence(self, session, context: AnalysisContext) -> bool:
        if not session.artifact_index_available:
            error_ids = (
                [session.artifact_index_error.error_id]
                if session.artifact_index_error is not None
                else []
            )
            self._skip_phase(
                session,
                PhaseCode.EVIDENCE_INDEX,
                reason="artifact_index_unavailable",
                error_ids=error_ids,
            )
            return True
        if self.evidence_service is None:
            if not self._start_phase(session, PhaseCode.EVIDENCE_INDEX):
                return False
            error = self._record_stage_error(
                session,
                phase=PhaseCode.EVIDENCE_INDEX,
                code="run_dispatch_failed",
                message="证据索引阶段尚未装配唯一执行 owner。",
            )
            self._finish_phase(
                session,
                PhaseCode.EVIDENCE_INDEX,
                timing_status=PhaseTimingStatus.FAILED,
                errors=[error],
                event_summary={"code": error.code},
            )
            return True
        if not self._start_phase(session, PhaseCode.EVIDENCE_INDEX):
            return False
        try:
            result = self.evidence_service.build_index(
                self._ordered_branch_results(session),
                session.integration_result,
                context,
            )
        except RunStopRequested:
            return False
        except Exception:
            result = None
        errors = self._stage_errors(session, PhaseCode.EVIDENCE_INDEX)
        valid = isinstance(result, EvidenceIndex) and result.run_id == session.run_id
        committed = bool(
            valid
            and session.artifact_index_available
            and any(
                entry.kind == "evidence_index" and entry.path == "evidence_index.json"
                for entry in session.artifact_index.entries
            )
        )
        if valid and committed:
            session.evidence_index = result
            session.evidence_index_committed = True
            session.integration_result.evidence_index = result
            timing_status = (
                PhaseTimingStatus.COMPLETED_WITH_ERRORS
                if any(item.severity == Severity.ERROR for item in errors)
                else PhaseTimingStatus.COMPLETED
            )
        else:
            if not errors and session.artifact_index_error is None:
                error = self._record_stage_error(
                    session,
                    phase=PhaseCode.EVIDENCE_INDEX,
                    code="evidence_index_build_failed",
                    message="证据索引阶段未形成完整稳定结果。",
                )
                errors = [error]
            timing_status = PhaseTimingStatus.FAILED
        self._finish_phase(
            session,
            PhaseCode.EVIDENCE_INDEX,
            timing_status=timing_status,
            errors=errors,
            event_summary={"evidence_count": len(result.evidence) if committed else 0},
        )
        return True

    def _commit_product_terminal(self, session) -> None:
        def command(context: RunCommitContext) -> None:
            if session.product_terminal_committed:
                return
            errors = session.ops.all_errors(context)
            deliverable = (
                session.final_project_data_list_committed
                and session.final_project_data_list is not None
                and session.summary.project_data_list_path is not None
            )
            status = RunStatus.FAILED
            if deliverable:
                status = (
                    RunStatus.COMPLETED_WITH_ERRORS
                    if any(item.severity == Severity.ERROR for item in errors)
                    else RunStatus.COMPLETED
                )
            now = self.clock()
            session.ops.persist_summary(
                context,
                terminal_summary(
                    session.summary,
                    status=status,
                    finished_at=now,
                    errors=errors,
                ),
            )
            session.product_terminal_committed = True
            session.control.close_business()

        session.channel.execute(command)

    def _run_result_pipeline(self, session) -> None:
        context = self._analysis_facades(session)
        if not self._run_integration(session, context):
            return
        if not self._run_rule_check(session, context):
            return
        if not self._run_evidence(session, context):
            return
        self._commit_product_terminal(session)
