from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from flyboard.models import (
    ArtifactIndex,
    BranchCode,
    BranchResult,
    ErrorRecord,
    EvidenceIndex,
    IntegrationResult,
    LayerDefinition,
    ProjectDataRow,
    RuleCheckResult,
    RuleSetSnapshot,
    RunInputSnapshot,
    RunSummary,
    ToolFinding,
)

from .commit import RunCommitChannel
from .control import RunControl
from .evidence_ids import RunEvidenceIdHelper

if TYPE_CHECKING:
    from flyboard.repositories import ArtifactRepository

    from .operations import (
        ArtifactWriter,
        DiagnosticRecorder,
        ModelCallRecorder,
        RunCommitOps,
        TrajectoryWriter,
    )


@dataclass(slots=True)
class GerberPreparationState:
    status: Literal["queued", "running", "completed", "failed"] = "queued"
    preparation: object | None = None
    error_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RunExecutionSession:
    """The only process-local mutable authority for one active run."""

    project_id: str
    run_id: str
    snapshot: RunInputSnapshot | None
    summary: RunSummary
    artifact_index: ArtifactIndex
    rule_set_snapshot: RuleSetSnapshot | None = None
    control: RunControl = field(default_factory=RunControl)
    channel: RunCommitChannel = field(default_factory=RunCommitChannel)
    diagnostics: DiagnosticRecorder = field(init=False, repr=False)
    model_call_recorder: ModelCallRecorder = field(init=False, repr=False)
    evidence_id_helper: RunEvidenceIdHelper = field(default_factory=RunEvidenceIdHelper, repr=False)
    ops: RunCommitOps = field(init=False, repr=False)
    artifact_writer: ArtifactWriter = field(init=False, repr=False)
    trajectory_writer: TrajectoryWriter = field(init=False, repr=False)
    branch_results: dict[BranchCode, BranchResult] = field(default_factory=dict)
    tool_findings: list[ToolFinding] | None = None
    tool_findings_committed: bool = False
    artifact_index_available: bool = True
    artifact_index_error: ErrorRecord | None = None
    committed_artifact_paths: set[str] = field(default_factory=set)
    preparation_states_by_job: dict[str, GerberPreparationState] = field(default_factory=dict)
    definitions_by_job: dict[str, LayerDefinition] = field(default_factory=dict)
    gerber_analysis_scheduled: bool = False
    integration_result: IntegrationResult | None = None
    integration_committed: bool = False
    rule_check_result: RuleCheckResult | None = None
    rule_check_result_committed: bool = False
    final_project_data_list: list[ProjectDataRow] | None = None
    final_project_data_list_committed: bool = False
    evidence_index: EvidenceIndex | None = None
    evidence_index_committed: bool = False
    product_terminal_committed: bool = False
    report_attempted: bool = False
    cleanup_started: bool = False
    registered: bool = False
    lifecycle_lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def assemble(
        cls,
        *,
        project_id: str,
        run_id: str,
        snapshot: RunInputSnapshot | None,
        rule_set_snapshot: RuleSetSnapshot | None,
        summary: RunSummary,
        artifact_index: ArtifactIndex,
        artifacts: ArtifactRepository,
        clock: Callable[[], datetime],
    ) -> RunExecutionSession:
        """Create the session and its one bound set of commit components."""
        from .operations import (
            ArtifactWriter,
            DiagnosticRecorder,
            ModelCallRecorder,
            RunCommitOps,
            TrajectoryWriter,
        )

        session = cls(
            project_id=project_id,
            run_id=run_id,
            snapshot=snapshot,
            rule_set_snapshot=rule_set_snapshot,
            summary=summary,
            artifact_index=artifact_index,
            artifact_index_available=summary.artifact_index_path is not None,
        )
        if snapshot is not None:
            session.preparation_states_by_job = {
                job_id: GerberPreparationState() for job_id in snapshot.groups.gerber_jobs
            }
        ops = RunCommitOps(session, artifacts, clock)
        session.ops = ops
        session.diagnostics = DiagnosticRecorder(session, ops)
        session.model_call_recorder = ModelCallRecorder(session, ops)
        session.artifact_writer = ArtifactWriter(session, ops)
        session.trajectory_writer = TrajectoryWriter(session, ops)
        return session

    def is_assembled(self) -> bool:
        components = (
            "ops",
            "diagnostics",
            "model_call_recorder",
            "artifact_writer",
            "trajectory_writer",
        )
        if not all(hasattr(self, name) for name in components):
            return False
        return (
            self.ops.session is self
            and self.diagnostics._session is self
            and self.model_call_recorder._session is self
            and self.artifact_writer._session is self
            and self.trajectory_writer._session is self
        )

    def release_large_objects(self) -> None:
        self.snapshot = None
        self.rule_set_snapshot = None
        self.branch_results.clear()
        self.tool_findings = None
        self.preparation_states_by_job.clear()
        self.definitions_by_job.clear()
        self.integration_result = None
        self.rule_check_result = None
        self.final_project_data_list = None
        self.evidence_index = None
        self.committed_artifact_paths.clear()


class ActiveSessionRegistry:
    """Identity-safe active-session registry; it does not decide run admission."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[tuple[str, str], RunExecutionSession] = {}

    def register(self, session: RunExecutionSession) -> None:
        if not session.is_assembled():
            raise RuntimeError("active run session is not fully assembled")
        key = (session.project_id, session.run_id)
        with self._lock:
            if key in self._sessions:
                raise RuntimeError("active run session already registered")
            self._sessions[key] = session
            session.registered = True

    def get(self, project_id: str, run_id: str) -> RunExecutionSession | None:
        with self._lock:
            return self._sessions.get((project_id, run_id))

    def unregister(self, session: RunExecutionSession) -> bool:
        key = (session.project_id, session.run_id)
        with self._lock:
            if self._sessions.get(key) is not session:
                return False
            del self._sessions[key]
            session.registered = False
            return True

    def snapshot(self) -> tuple[RunExecutionSession, ...]:
        with self._lock:
            return tuple(self._sessions.values())
