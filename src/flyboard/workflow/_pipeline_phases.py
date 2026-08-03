from __future__ import annotations

from flyboard.models import (
    BranchCode,
    BranchResult,
    PhaseCode,
    PhaseTiming,
    PhaseTimingStatus,
    RuleCheckStatus,
    RuleCheckSummary,
    RunStatus,
    ScopeType,
    Severity,
    TrajectoryEvent,
    TrajectoryEventType,
)

from ._state import BRANCH_ORDER, AnalysisContext, duration_ms
from .commit import CommitContractError, RunCommitContext
from .operations import _validated_summary


class PipelinePhaseMixin:
    def _analysis_facades(
        self,
        session,
        *,
        branch: BranchCode | None = None,
    ) -> AnalysisContext:
        project_root = self.config.storage_root / "projects" / session.project_id
        return AnalysisContext(
            project_id=session.project_id,
            run_id=session.run_id,
            project_root=project_root,
            output_dir=project_root / "runs" / session.run_id,
            config=self.config,
            stop_signal=session.control.stop_signal,
            artifact_writer=(
                session.artifact_writer
                if branch is None
                else session.artifact_writer.for_branch(branch)
            ),
            trajectory_writer=session.trajectory_writer,
            diagnostics=session.diagnostics,
            model_client=self.model_client,
            model_calls=session.model_call_recorder,
            evidence_id_helper=session.evidence_id_helper,
        )

    @staticmethod
    def _ordered_branch_results(session) -> list[BranchResult]:
        return [
            session.branch_results[branch]
            for branch in BRANCH_ORDER
            if branch in session.branch_results
        ]

    def _start_phase(self, session, phase: PhaseCode, *, rule_running: bool = False) -> bool:
        def command(context: RunCommitContext) -> bool:
            if (
                session.summary.status != RunStatus.RUNNING
                or session.summary.stop_requested_at is not None
                or not session.control.accepts_business()
            ):
                return False
            now = self.clock()
            timing = PhaseTiming(
                phase=phase,
                scope_type=ScopeType.PHASE,
                scope_id=None,
                status=PhaseTimingStatus.RUNNING,
                started_at=now,
                finished_at=None,
                duration_ms=None,
            )
            values = {
                "current_phase": phase,
                "phase_timings": [*session.summary.phase_timings, timing],
            }
            if rule_running:
                selection = session.snapshot.rule_set
                if selection.mode != "selected":
                    raise CommitContractError("running rule phase requires a selected rule set")
                values["rule_check_summary"] = RuleCheckSummary(
                    status=RuleCheckStatus.RUNNING,
                    rule_set_id=selection.rule_set_id,
                    rule_set_version=selection.rule_set_version,
                    attempted_rule_count=None,
                    error_count=None,
                    reason=None,
                )
            session.ops.persist_summary(context, _validated_summary(session.summary, **values))
            session.ops.append_trajectory(
                context,
                TrajectoryEvent(
                    schema_version="1.0",
                    run_id=session.run_id,
                    timestamp=now,
                    phase=phase,
                    branch=None,
                    event_type=TrajectoryEventType.PHASE_STARTED,
                    summary={"phase": phase.value},
                    source_material_ids=[],
                    artifact_paths=[],
                    error_ids=[],
                ),
            )
            return True

        return session.channel.execute(command)

    def _enter_rule_skip(self, session) -> bool:
        def command(context: RunCommitContext) -> bool:
            if (
                session.summary.status != RunStatus.RUNNING
                or session.summary.stop_requested_at is not None
                or not session.control.accepts_business()
            ):
                return False
            session.ops.persist_summary(
                context,
                _validated_summary(session.summary, current_phase=PhaseCode.RULE_CHECK),
            )
            return True

        return session.channel.execute(command)

    def _finish_phase(
        self,
        session,
        phase: PhaseCode,
        *,
        timing_status: PhaseTimingStatus,
        errors,
        summary_values: dict | None = None,
        event_summary: dict | None = None,
    ) -> None:
        def command(context: RunCommitContext) -> None:
            canonical_errors = []
            seen = set()
            for error in errors:
                canonical = session.ops.validate_error_identity(context, error)
                if canonical.error_id not in seen:
                    canonical_errors.append(canonical)
                    seen.add(canonical.error_id)
            now = self.clock()
            timings = []
            final_timing = None
            for timing in session.summary.phase_timings:
                if (
                    timing.phase == phase
                    and timing.scope_type == ScopeType.PHASE
                    and timing.scope_id is None
                    and timing.status == PhaseTimingStatus.RUNNING
                ):
                    final_timing = PhaseTiming(
                        phase=phase,
                        scope_type=ScopeType.PHASE,
                        scope_id=None,
                        status=timing_status,
                        started_at=timing.started_at,
                        finished_at=now,
                        duration_ms=duration_ms(timing.started_at, now),
                    )
                    timings.append(final_timing)
                else:
                    timings.append(timing)
            if final_timing is None:
                raise CommitContractError(f"{phase.value} completion has no running timing")
            merged = list(session.summary.error_summary)
            known = {item.error_id for item in merged}
            for error in canonical_errors:
                if error.error_id not in known:
                    merged.append(error)
                    known.add(error.error_id)
            values = dict(summary_values or {})
            values.update(phase_timings=timings, error_summary=merged)
            session.ops.persist_summary(context, _validated_summary(session.summary, **values))
            session.ops.append_trajectory(
                context,
                TrajectoryEvent(
                    schema_version="1.0",
                    run_id=session.run_id,
                    timestamp=now,
                    phase=phase,
                    branch=None,
                    event_type=(
                        TrajectoryEventType.PHASE_FAILED
                        if timing_status == PhaseTimingStatus.FAILED
                        else TrajectoryEventType.PHASE_FINISHED
                    ),
                    summary=event_summary or {"phase": phase.value},
                    duration_ms=final_timing.duration_ms,
                    source_material_ids=[],
                    artifact_paths=[],
                    error_ids=[item.error_id for item in canonical_errors],
                ),
            )

        session.channel.execute(command)

    def _skip_phase(
        self,
        session,
        phase: PhaseCode,
        *,
        reason: str,
        error_ids: list[str],
    ) -> None:
        def command(context: RunCommitContext) -> None:
            errors = [session.ops.resolve_error(context, error_id) for error_id in error_ids]
            now = self.clock()
            timing = PhaseTiming(
                phase=phase,
                scope_type=ScopeType.PHASE,
                scope_id=None,
                status=PhaseTimingStatus.SKIPPED,
                started_at=now,
                finished_at=now,
                duration_ms=0,
            )
            session.ops.persist_summary(
                context,
                _validated_summary(
                    session.summary,
                    current_phase=phase,
                    phase_timings=[*session.summary.phase_timings, timing],
                ),
            )
            session.ops.append_trajectory(
                context,
                TrajectoryEvent(
                    schema_version="1.0",
                    run_id=session.run_id,
                    timestamp=now,
                    phase=phase,
                    branch=None,
                    event_type=TrajectoryEventType.PHASE_SKIPPED,
                    summary={"reason": reason},
                    duration_ms=0,
                    source_material_ids=[],
                    artifact_paths=[],
                    error_ids=[item.error_id for item in errors],
                ),
            )

        session.channel.execute(command)

    def _stage_errors(self, session, phase: PhaseCode):
        return session.channel.execute(
            lambda context: [
                error for error in session.ops.all_errors(context) if error.phase == phase
            ]
        )

    def _record_stage_error(self, session, *, phase: PhaseCode, code: str, message: str):
        return session.channel.execute(
            lambda context: session.ops.record_error(
                context,
                code=code,
                severity=Severity.ERROR,
                phase=phase,
                branch=None,
                message=message,
                source_material_ids=[],
                artifact_paths=[],
                suggested_action="检查对应阶段实现与受管产物后创建新的运行。",
            )
        )

    def _fail_started_stage(self, session, phase: PhaseCode, error) -> None:
        self._finish_phase(
            session,
            phase,
            timing_status=PhaseTimingStatus.FAILED,
            errors=[error],
            event_summary={"code": error.code},
        )
        self._fail_session(
            session,
            code=error.code,
            phase=phase,
            branch=None,
            message=error.message,
            source_material_ids=[],
            suggested_action=error.suggested_action,
            existing_error_id=error.error_id,
        )
