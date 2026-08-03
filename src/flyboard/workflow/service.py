from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Executor, ThreadPoolExecutor

from flyboard.config import AppConfig
from flyboard.errors import ServiceError
from flyboard.evidence import DeterministicEvidenceService
from flyboard.gerber import GerberScoutAnalyzer
from flyboard.integration import SingleXlsxIntegrator
from flyboard.model_calls import ModelCaller, UnavailableModelCaller
from flyboard.models import (
    AccessLevel,
    ArtifactConsumer,
    ArtifactIndex,
    ArtifactIndexEntry,
    BranchCode,
    BranchStatus,
    BranchStatusMap,
    PhaseCode,
    PhaseTiming,
    PhaseTimingStatus,
    ProjectRecord,
    RuleCheckStatus,
    RuleCheckSummary,
    RunInputSnapshot,
    RunStatus,
    RunSummary,
    RunSummaryResponse,
    ScopeType,
    materialize_run_summary,
)
from flyboard.pdf_image import PdfImageAnalyzer
from flyboard.repositories import ArtifactRepository, ProjectRepository
from flyboard.rules import RuleEngine as ProductionRuleEngine
from flyboard.services._support import new_id, utc_now
from flyboard.services.layer_definitions import LayerDefinitionService
from flyboard.services.rule_sets import RuleSetService
from flyboard.xlsx_analysis import XlsxAnalyzer

from ._branches import BranchExecutionMixin
from ._coordinator import CoordinatorMixin
from ._gerber import GerberCoordinationMixin
from ._layers import LayerDefinitionBuilder, LayerDefinitionCommandsMixin
from ._lifecycle import LifecycleCommandsMixin
from ._reads import ReadCommandsMixin
from ._result_pipeline import ResultPipelineMixin
from ._state import (
    BranchWorker,
    EvidenceService,
    GerberAnalyzer,
    Integrator,
    ReportRenderer,
    RuleEngine,
    duration_ms,
)
from ._tail import TerminalTailMixin
from .input_snapshots import _InputSnapshotBuilder
from .session import (
    ActiveSessionRegistry,
    RunExecutionSession,
)


class RunService(
    BranchExecutionMixin,
    GerberCoordinationMixin,
    CoordinatorMixin,
    ResultPipelineMixin,
    LifecycleCommandsMixin,
    TerminalTailMixin,
    ReadCommandsMixin,
    LayerDefinitionCommandsMixin,
):
    """The sole application facade and state-transition owner for v1 runs."""

    def __init__(
        self,
        projects: ProjectRepository,
        artifacts: ArtifactRepository,
        rule_sets: RuleSetService,
        *,
        branch_workers: Mapping[BranchCode, BranchWorker] | None = None,
        model_client: ModelCaller | None = None,
        gerber_analyzer: GerberAnalyzer | None = None,
        layer_definitions: LayerDefinitionBuilder | None = None,
        integrator: Integrator | None = None,
        rule_engine: RuleEngine | None = None,
        evidence_service: EvidenceService | None = None,
        report_renderer: ReportRenderer | None = None,
        executor: Executor | None = None,
        clock=utc_now,
        id_factory: Callable[[str], str] = new_id,
        thread_factory=threading.Thread,
        recover_on_startup: bool = True,
    ) -> None:
        self.projects = projects
        self.artifacts = artifacts
        self.config = AppConfig(artifacts.storage_root)
        self._input_snapshots = _InputSnapshotBuilder(projects, rule_sets)
        provided_workers = dict(branch_workers or {})
        default_pipeline = (
            not provided_workers
            and integrator is None
            and rule_engine is None
            and evidence_service is None
        )
        self.branch_workers = provided_workers
        self.branch_workers.setdefault(BranchCode.PDF_IMAGE, PdfImageAnalyzer(projects).analyze)
        self.branch_workers.setdefault(BranchCode.XLSX, XlsxAnalyzer(projects, clock=clock).analyze)
        self.model_client = model_client or UnavailableModelCaller()
        if BranchCode.GERBER in self.branch_workers:
            raise ValueError(
                "Gerber analysis requires the batch-D preparation and confirmation gate"
            )
        self.gerber_analyzer = gerber_analyzer or GerberScoutAnalyzer(projects)
        self.layer_definitions = layer_definitions or LayerDefinitionService()
        self.integrator = SingleXlsxIntegrator() if default_pipeline else integrator
        self.rule_engine = ProductionRuleEngine() if default_pipeline else rule_engine
        self.evidence_service = (
            DeterministicEvidenceService(artifacts) if default_pipeline else evidence_service
        )
        self.report_renderer = report_renderer
        self.executor = executor or ThreadPoolExecutor(
            max_workers=3, thread_name_prefix="flyboard-worker"
        )
        self._owns_executor = executor is None
        self.clock = clock
        self.id_factory = id_factory
        self.thread_factory = thread_factory
        self._admission = threading.Lock()
        self._registry = ActiveSessionRegistry()
        self._coordinator_guard = threading.Lock()
        self._coordinators: dict[tuple[str, str], threading.Thread] = {}
        self._startup_ready = False
        if recover_on_startup:
            self.recover_startup()
        else:
            self._startup_ready = True

    def _make_session(
        self,
        *,
        project_id,
        run_id,
        snapshot,
        rule_set_snapshot,
        summary,
        artifact_index,
    ) -> RunExecutionSession:
        return RunExecutionSession.assemble(
            project_id=project_id,
            run_id=run_id,
            snapshot=snapshot,
            rule_set_snapshot=rule_set_snapshot,
            summary=summary,
            artifact_index=artifact_index,
            artifacts=self.artifacts,
            clock=self.clock,
        )

    def _new_run_id(self, project_id: str, existing_ids: set[str]) -> str:
        for _ in range(100):
            candidate = self.id_factory("run")
            if candidate in existing_ids:
                continue
            if not self.artifacts._run_root(project_id, candidate).exists():
                return candidate
        raise ServiceError("run_id_allocation_failed")

    def _active_run(self):
        for record in self.projects.list_records():
            for summary in self.artifacts.list_run_summaries(record.project_id):
                if summary.status in {RunStatus.QUEUED, RunStatus.RUNNING}:
                    return record.project_id, summary
        return None

    @staticmethod
    def _initial_rule_summary(snapshot: RunInputSnapshot) -> RuleCheckSummary:
        if snapshot.rule_set.mode == "not_selected":
            return RuleCheckSummary(
                status=RuleCheckStatus.SKIPPED,
                rule_set_id=None,
                rule_set_version=None,
                attempted_rule_count=0,
                error_count=0,
                reason="rule_set_not_selected",
            )
        return RuleCheckSummary(
            status=RuleCheckStatus.NOT_REACHED,
            rule_set_id=snapshot.rule_set.rule_set_id,
            rule_set_version=snapshot.rule_set.rule_set_version,
            attempted_rule_count=0,
            error_count=0,
            reason="awaiting_rule_check",
        )

    def _initial_summary(self, project_id, run_id, snapshot, now) -> RunSummary:
        return RunSummary(
            schema_version="1.0",
            run_id=run_id,
            project_id=project_id,
            status=RunStatus.QUEUED,
            current_phase=None,
            phase_timings=[
                PhaseTiming(
                    phase=PhaseCode.INPUT_SNAPSHOT,
                    scope_type=ScopeType.PHASE,
                    scope_id=None,
                    status=PhaseTimingStatus.RUNNING,
                    started_at=now,
                    finished_at=None,
                    duration_ms=None,
                )
            ],
            branch_status=BranchStatusMap(
                pdf_image=BranchStatus.QUEUED,
                xlsx=BranchStatus.QUEUED,
                gerber=BranchStatus.QUEUED,
            ),
            rule_check_summary=self._initial_rule_summary(snapshot),
            model_call_summaries=[],
            error_summary=[],
            stop_requested_at=None,
            stop_reason=None,
            started_at=now,
            finished_at=None,
            duration_ms=None,
            artifact_index_path="artifact_index.json",
            project_data_list_path=None,
        )

    @staticmethod
    def _snapshot_entry(snapshot) -> ArtifactIndexEntry:
        return ArtifactIndexEntry(
            path="run_input_snapshot.json",
            kind="run_input_snapshot",
            producer_phase=PhaseCode.INPUT_SNAPSHOT,
            consumers=[ArtifactConsumer.WORKFLOW, ArtifactConsumer.REGRESSION],
            access_level=AccessLevel.RESTRICTED_DEBUG,
            source_material_ids=[item.material_id for item in snapshot.materials],
        )

    @staticmethod
    def _rule_snapshot_entry() -> ArtifactIndexEntry:
        return ArtifactIndexEntry(
            path="rules/rule_set_snapshot.json",
            kind="rule_set_snapshot",
            producer_phase=PhaseCode.INPUT_SNAPSHOT,
            consumers=[ArtifactConsumer.WORKFLOW, ArtifactConsumer.REGRESSION],
            access_level=AccessLevel.RESTRICTED_DEBUG,
            source_material_ids=[],
        )

    def create_run(
        self, project_id: str, material_ids: list[str], rule_set_id: str | None
    ) -> RunSummaryResponse:
        if not self._startup_ready:
            raise ServiceError("service_startup_incomplete")
        with self._admission:
            with self.projects.mutate(project_id) as mutation:
                current = mutation.get_current()
                conflict_now = self.clock()
                active = self._active_run()
                if active is not None:
                    active_project_id, active_summary = active
                    raise ServiceError(
                        "active_run_exists",
                        active_project_id=active_project_id,
                        active_run_id=active_summary.run_id,
                        run_summary=materialize_run_summary(active_summary, now=conflict_now),
                    )
                prepared = self._input_snapshots.prepare(current, material_ids, rule_set_id)
                existing_ids = {
                    item.run_id
                    for record in self.projects.list_records()
                    for item in self.artifacts.list_run_summaries(record.project_id)
                }
                run_id = self._new_run_id(project_id, existing_ids)
                snapshot = self._input_snapshots.build_snapshot(prepared, run_id)
                started_at = self.clock()
                summary = self._initial_summary(project_id, run_id, snapshot, started_at)
                index = ArtifactIndex(schema_version="1.0", run_id=run_id, entries=[])
                session = self._make_session(
                    project_id=project_id,
                    run_id=run_id,
                    snapshot=snapshot,
                    rule_set_snapshot=prepared.rule_set_snapshot,
                    summary=summary,
                    artifact_index=index,
                )
                summary_written = False
                try:
                    self.artifacts.replace_run_summary(project_id, run_id, summary)
                    summary_written = True
                    self.artifacts.replace_artifact_index(project_id, run_id, index)
                    if prepared.rule_set_snapshot is not None:
                        self._write_creation_artifact(
                            session,
                            prepared.rule_set_snapshot,
                            self._rule_snapshot_entry(),
                        )
                    self._write_creation_artifact(session, snapshot, self._snapshot_entry(snapshot))
                    finished_at = self.clock()
                    input_timing = PhaseTiming(
                        phase=PhaseCode.INPUT_SNAPSHOT,
                        scope_type=ScopeType.PHASE,
                        scope_id=None,
                        status=(
                            PhaseTimingStatus.COMPLETED_WITH_ERRORS
                            if session.artifact_index_error is not None
                            else PhaseTimingStatus.COMPLETED
                        ),
                        started_at=started_at,
                        finished_at=finished_at,
                        duration_ms=duration_ms(started_at, finished_at),
                    )
                    session.channel.execute(
                        lambda context: session.ops.persist_summary(
                            context,
                            RunSummary.model_validate(
                                {
                                    **session.summary.model_dump(mode="python"),
                                    "phase_timings": [input_timing],
                                }
                            ),
                        )
                    )
                    next_record = ProjectRecord.model_validate(
                        {
                            **current.model_dump(mode="python"),
                            "latest_run_id": run_id,
                            "updated_at": self.clock(),
                        }
                    )
                    mutation.commit(
                        next_record,
                        expected_latest_run_id=current.latest_run_id,
                    )
                except Exception as exc:
                    if summary_written:
                        self._compensate_creation_failure(session, exc)
                    else:
                        self._release_session(session)
                    raise

                queued_response = materialize_run_summary(
                    session.summary.model_copy(deep=True), now=self.clock()
                )
                try:
                    self._registry.register(session)
                    self._start_coordinator(session)
                except Exception:
                    self._fail_session(
                        session,
                        code="run_dispatch_failed",
                        phase=None,
                        branch=None,
                        message="运行已经创建，但后台协调器未能启动。",
                        source_material_ids=[],
                        suggested_action="检查本地执行资源后创建新的运行。",
                    )
                    failed = materialize_run_summary(session.summary, now=self.clock())
                    self._run_terminal_tail(session)
                    return failed
                return queued_response

    @staticmethod
    def _write_creation_artifact(session, artifact, entry) -> None:
        session.artifact_writer.write_json(artifact, entry)

    def _compensate_creation_failure(self, session, failure: Exception) -> None:
        existing_error_id = (
            failure.error_id
            if isinstance(failure, ServiceError) and failure.error_id is not None
            else None
        )
        try:
            self._fail_session(
                session,
                code="run_creation_failed",
                phase=None,
                branch=None,
                message="运行创建在项目提交前失败，项目 latest 未更新。",
                source_material_ids=(
                    [item.material_id for item in session.snapshot.materials]
                    if session.snapshot is not None
                    else []
                ),
                suggested_action="修正存储或输入问题后重新创建运行。",
                existing_error_id=existing_error_id,
            )
        except Exception:
            session.control.close_business()
        finally:
            if session.product_terminal_committed:
                self._run_terminal_tail(session)
            else:
                self._release_session(session)

    def _start_coordinator(self, session) -> None:
        thread = self.thread_factory(
            target=self._coordinator_main,
            args=(session,),
            name=f"flyboard-coordinator-{session.run_id}",
            daemon=True,
        )
        key = (session.project_id, session.run_id)
        with self._coordinator_guard:
            self._coordinators[key] = thread
        try:
            thread.start()
        except Exception:
            with self._coordinator_guard:
                self._coordinators.pop(key, None)
            raise
