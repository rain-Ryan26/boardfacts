from __future__ import annotations

import time

from flyboard.errors import ServiceError
from flyboard.gerber import GerberScoutAnalyzer
from flyboard.models import (
    ArtifactIndex,
    RunStatus,
    RunSummaryResponse,
    TrajectoryEvent,
    TrajectoryEventType,
    materialize_run_summary,
)

from ._state import terminal_summary
from .commit import RunCommitContext
from .operations import _validated_summary


class LifecycleCommandsMixin:
    def request_stop(self, project_id: str, run_id: str) -> RunSummaryResponse:
        session = self._registry.get(project_id, run_id)
        if session is None:
            with self.projects.guard_product_run(project_id, run_id):
                summary = self.artifacts.read_run_summary(project_id, run_id)
            if summary.status == RunStatus.STOPPED:
                return materialize_run_summary(summary, now=self.clock())
            if summary.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
                raise ServiceError("run_state_conflict")
            raise ServiceError("run_session_unavailable")

        def command(context: RunCommitContext):
            current = session.summary
            if current.status == RunStatus.STOPPED:
                return current
            if current.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
                raise ServiceError("run_state_conflict")
            if current.stop_requested_at is not None:
                return current
            now = self.clock()
            if current.status == RunStatus.QUEUED:
                session.ops.persist_summary(
                    context,
                    terminal_summary(
                        current,
                        status=RunStatus.STOPPED,
                        finished_at=now,
                        stop_requested_at=now,
                    ),
                )
                session.product_terminal_committed = True
                session.control.request_user_stop()
                event_type = TrajectoryEventType.RUN_STOPPED
                phase = None
            else:
                session.ops.persist_summary(
                    context, _validated_summary(current, stop_requested_at=now)
                )
                session.control.request_user_stop()
                event_type = TrajectoryEventType.STOP_REQUESTED
                phase = current.current_phase
            try:
                session.ops.append_trajectory(
                    context,
                    TrajectoryEvent(
                        schema_version="1.0",
                        run_id=run_id,
                        timestamp=now,
                        phase=phase,
                        branch=None,
                        event_type=event_type,
                        summary={"reason": "user_requested"},
                        source_material_ids=[],
                        artifact_paths=[],
                        error_ids=[],
                    ),
                )
            except ServiceError:
                pass
            return session.summary

        summary = session.channel.execute(command)
        session.control.wake()
        if summary.status == RunStatus.STOPPED:
            self._run_terminal_tail(session)
        return materialize_run_summary(summary, now=self.clock())

    def recover_startup(self) -> None:
        self._startup_ready = False
        if self.projects.retry_pending_deletions():
            raise ServiceError("project_cleanup_incomplete")
        for record in self.projects.list_records():
            for summary in self.artifacts.list_run_summaries(record.project_id):
                if summary.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
                    continue
                index = ArtifactIndex(schema_version="1.0", run_id=summary.run_id, entries=[])
                if summary.artifact_index_path is not None:
                    try:
                        index = self.artifacts.read_artifact_index(
                            record.project_id,
                            summary.run_id,
                            summary.artifact_index_path,
                        )
                    except ServiceError:
                        pass
                session = self._make_session(
                    project_id=record.project_id,
                    run_id=summary.run_id,
                    snapshot=None,
                    rule_set_snapshot=None,
                    summary=summary,
                    artifact_index=index,
                )
                if summary.status == RunStatus.RUNNING and summary.stop_requested_at is not None:
                    session.control.request_user_stop()
                    self._finalize_stop(session)
                else:
                    self._fail_session(
                        session,
                        code="service_interrupted",
                        phase=summary.current_phase,
                        branch=None,
                        message="服务启动扫描发现未完成的旧运行。",
                        source_material_ids=[],
                        suggested_action="创建新的运行。",
                        service_interrupted=True,
                    )
                self._run_terminal_tail(session)
        self._startup_ready = True

    def shutdown(self, grace_seconds: float = 5.0) -> None:
        sessions = self._registry.snapshot()
        for session in sessions:

            def command(context: RunCommitContext, current=session) -> None:
                current.channel.assert_context(context)
                if current.summary.status not in {
                    RunStatus.QUEUED,
                    RunStatus.RUNNING,
                }:
                    return
                if current.summary.stop_requested_at is not None:
                    current.control.request_user_stop()
                else:
                    current.control.request_service_shutdown()

            session.channel.execute(command)
            session.control.wake()
        deadline = time.monotonic() + max(0.0, grace_seconds)
        for session in sessions:
            key = (session.project_id, session.run_id)
            with self._coordinator_guard:
                thread = self._coordinators.get(key)
            if thread is None:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)
        if self._owns_executor:
            self.executor.shutdown(wait=False, cancel_futures=False)

    def _release_session(self, session) -> None:
        with session.lifecycle_lock:
            if session.cleanup_started:
                return
            session.cleanup_started = True
            if isinstance(self.gerber_analyzer, GerberScoutAnalyzer):
                self.gerber_analyzer.release_run(session.run_id)
            session.release_large_objects()
            self._registry.unregister(session)
