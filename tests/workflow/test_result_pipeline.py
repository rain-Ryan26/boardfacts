from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from threading import Event

from flyboard.errors import ServiceError
from flyboard.models import (
    AccessLevel,
    ArtifactConsumer,
    ArtifactIndexEntry,
    BranchCode,
    BranchResult,
    EvidenceIndex,
    IntegrationResult,
    PhaseCode,
    ProjectDataListArtifact,
    ProjectDataRow,
    ProjectMaterial,
    ProjectRecord,
    RuleCheckResult,
    RunStatus,
    build_current_field_table,
)
from flyboard.repositories import ArtifactRepository, ProjectRepository
from flyboard.services import RuleSetService
from flyboard.workflow import AnalysisContext, RunService

NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


def artifact_entry(path, kind, phase, consumers, access=AccessLevel.RESTRICTED_DEBUG):
    return ArtifactIndexEntry(
        path=path,
        kind=kind,
        producer_phase=phase,
        consumers=consumers,
        access_level=access,
        source_material_ids=[],
    )


def branch_worker(snapshot, context):
    assert isinstance(context, AnalysisContext)
    assert context.project_root == (context.config.storage_root / "projects" / context.project_id)
    assert context.output_dir == context.project_root / "runs" / context.run_id
    return BranchResult.model_validate(
        {
            "schema_version": "1.0",
            "run_id": snapshot.run_id,
            "branch": "pdf_image",
            "status": "completed",
            "source_material_ids": ["mat_1"],
            "fields": [
                {
                    "field_code": "board_thickness_mm",
                    "field_name": "成品板厚",
                    "field_value": "1.6",
                    "unit": "mm",
                    "category": "板件设计参数",
                    "branch_state": "clear_now",
                    "source": "pdf_ai",
                    "source_material_id": "mat_1",
                    "source_ref": "PDF_1_page1",
                    "evidence_ids": [],
                    "tool_finding_ids": [],
                    "error_ids": [],
                    "check_note": "测试候选。",
                }
            ],
            "unknown_notes": [],
            "tool_findings": [],
            "evidence": [],
            "errors": [],
        }
    )


def final_row():
    return ProjectDataRow(
        index=1,
        field_code="board_thickness_mm",
        field_name="成品板厚",
        field_value="1.6",
        source="PDF_1_page1",
        evidence_ids=[],
        tool_finding_ids=[],
        error_ids=[],
        conflict_candidates=[],
        category="板件设计参数",
        field_state="confirmed",
        check_note="测试候选。",
        check_conclusion="检查通过",
    )


class PipelineIntegrator:
    def __init__(self):
        self.calls = 0
        self.inputs = None

    def merge(self, snapshot, branch_results, tool_findings, context):
        self.calls += 1
        self.inputs = (snapshot, list(branch_results), tool_findings)
        result = IntegrationResult(
            draft_project_data_list=[final_row()],
            project_data_list=None,
            tool_findings=tool_findings,
            errors=[],
            evidence_index=None,
        )
        try:
            context.artifact_writer.write_json(
                ProjectDataListArtifact(
                    schema_version="1.0",
                    run_id=snapshot.run_id,
                    rows=result.draft_project_data_list,
                ),
                artifact_entry(
                    "integration/draft_project_data_list.json",
                    "draft_project_data_list",
                    PhaseCode.INTEGRATION,
                    [ArtifactConsumer.WORKFLOW, ArtifactConsumer.REGRESSION],
                ),
            )
        except ServiceError as exc:
            if exc.code != "artifact_index_update_failed":
                raise
        return result


class PipelineRuleEngine:
    def __init__(self):
        self.calls = 0
        self.rule_snapshot_inputs = []

    def check(self, snapshot, integration_result, rule_set_snapshot, context):
        self.calls += 1
        self.rule_snapshot_inputs.append(rule_set_snapshot)
        integration_result.project_data_list = list(integration_result.draft_project_data_list)
        context.artifact_writer.write_json(
            ProjectDataListArtifact(
                schema_version="1.0",
                run_id=snapshot.run_id,
                rows=integration_result.project_data_list,
            ),
            artifact_entry(
                "project_data_list.json",
                "project_data_list",
                PhaseCode.RULE_CHECK,
                [
                    ArtifactConsumer.API,
                    ArtifactConsumer.GUI,
                    ArtifactConsumer.REPORT,
                    ArtifactConsumer.REGRESSION,
                ],
                AccessLevel.PROJECT_SCOPED,
            ),
        )
        result = RuleCheckResult(
            schema_version="1.0",
            run_id=snapshot.run_id,
            status="skipped",
            reason="rule_set_not_selected",
            rule_set=None,
            executed_rule_ids=[],
            attempted_rule_count=0,
            errors=[],
        )
        context.artifact_writer.write_json(
            result,
            artifact_entry(
                "rules/rule_check_result.json",
                "rule_check_result",
                PhaseCode.RULE_CHECK,
                [ArtifactConsumer.REPORT, ArtifactConsumer.REGRESSION],
                AccessLevel.PROJECT_SCOPED,
            ),
        )
        return result


class SelectedRuleEngine(PipelineRuleEngine):
    def check(self, snapshot, integration_result, rule_set_snapshot, context):
        self.calls += 1
        self.rule_snapshot_inputs.append(rule_set_snapshot)
        assert rule_set_snapshot is not None
        integration_result.project_data_list = list(integration_result.draft_project_data_list)
        context.artifact_writer.write_json(
            ProjectDataListArtifact(
                schema_version="1.0",
                run_id=snapshot.run_id,
                rows=integration_result.project_data_list,
            ),
            artifact_entry(
                "project_data_list.json",
                "project_data_list",
                PhaseCode.RULE_CHECK,
                [ArtifactConsumer.API, ArtifactConsumer.GUI, ArtifactConsumer.REPORT],
                AccessLevel.PROJECT_SCOPED,
            ),
        )
        result = RuleCheckResult(
            schema_version="1.0",
            run_id=snapshot.run_id,
            status="completed",
            reason=None,
            rule_set={
                "rule_set_id": rule_set_snapshot.rule_set_id,
                "rule_set_version": rule_set_snapshot.rule_set_version,
                "content_sha256": rule_set_snapshot.content_sha256,
            },
            executed_rule_ids=[item.rule_id for item in rule_set_snapshot.rules],
            attempted_rule_count=0,
            errors=[],
        )
        context.artifact_writer.write_json(
            result,
            artifact_entry(
                "rules/rule_check_result.json",
                "rule_check_result",
                PhaseCode.RULE_CHECK,
                [ArtifactConsumer.REPORT, ArtifactConsumer.REGRESSION],
                AccessLevel.PROJECT_SCOPED,
            ),
        )
        return result


class PipelineEvidenceService:
    def __init__(self, *, fail=False):
        self.calls = 0
        self.fail = fail

    def build_index(self, branch_results, integration_result, context):
        self.calls += 1
        if self.fail:
            context.diagnostics.record_error(
                code="evidence_index_build_failed",
                severity="error",
                phase=PhaseCode.EVIDENCE_INDEX,
                branch=None,
                message="测试证据索引失败。",
                source_material_ids=[],
                artifact_paths=[],
                suggested_action="检查测试输入。",
            )
            return None
        result = EvidenceIndex(
            schema_version="1.0",
            run_id=context.run_id,
            evidence={},
            locators={},
        )
        context.artifact_writer.write_json(
            result,
            artifact_entry(
                "evidence_index.json",
                "evidence_index",
                PhaseCode.EVIDENCE_INDEX,
                [ArtifactConsumer.API, ArtifactConsumer.REPORT],
                AccessLevel.PROJECT_SCOPED,
            ),
        )
        return result

    def get(self, project_id, run_id, evidence_index, evidence_id):
        raise AssertionError("workflow test does not call EvidenceService.get")

    def get_content(
        self,
        project_id,
        run_id,
        artifact_index,
        evidence_index,
        evidence_id,
    ):
        raise AssertionError("workflow test does not call EvidenceService.get_content")


class RecordingRenderer:
    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    def render(
        self,
        run_id,
        project_data_list,
        rule_check_result,
        tool_findings,
        evidence_index,
    ):
        self.calls.append(
            (run_id, project_data_list, rule_check_result, tool_findings, evidence_index)
        )
        if self.fail:
            raise RuntimeError("render failed")
        return f"# 测试报告\n\nrun_id: {run_id}\n"


class FailDraftIndex(ArtifactRepository):
    def __init__(self, storage_root):
        super().__init__(storage_root)
        self.failed = False

    def replace_artifact_index(self, project_id, run_id, index):
        if not self.failed and any(
            item.path == "integration/draft_project_data_list.json" for item in index.entries
        ):
            self.failed = True
            raise ServiceError("artifact_index_update_failed")
        return super().replace_artifact_index(project_id, run_id, index)


def make_runtime(
    tmp_path,
    *,
    integrator=None,
    rule_engine=None,
    evidence_service=None,
    report_renderer=None,
    artifact_type=ArtifactRepository,
    register_rule_set=False,
):
    projects = ProjectRepository(tmp_path)
    material = ProjectMaterial(
        material_id="mat_1",
        kind="pdf",
        source_ref="sources/mat_1/input.pdf",
        display_name="input.pdf",
        registered_order=1,
        registered_at=NOW,
    )
    with projects.mutate("prj_test") as mutation:
        mutation.create(
            ProjectRecord(
                project_id="prj_test",
                project_name="结果链测试",
                created_at=NOW,
                updated_at=NOW,
                materials=[material],
                latest_run_id=None,
            )
        )
    source = tmp_path / "projects" / "prj_test" / "sources" / "mat_1" / "input.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF-1.4")
    artifacts = artifact_type(tmp_path)
    rule_sets = RuleSetService(tmp_path, clock=lambda: NOW)
    if register_rule_set:
        source = tmp_path / "rules.json"
        source.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "rule_set_id": "factory_rules",
                    "rule_set_name": "测试规则库",
                    "rule_set_version": "2026.07",
                    "description": "结果链规则快照测试",
                    "rules": [
                        {
                            "rule_id": "rule_1",
                            "name": "板厚检查",
                            "target_field_code": "board_thickness_mm",
                            "instruction": "检查板厚。",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        assert (
            rule_sets.register_file(str(source), build_current_field_table()).validation_status
            == "valid"
        )
    service = RunService(
        projects,
        artifacts,
        rule_sets,
        branch_workers={BranchCode.PDF_IMAGE: branch_worker},
        integrator=integrator,
        rule_engine=rule_engine,
        evidence_service=evidence_service,
        report_renderer=report_renderer,
        clock=lambda: NOW,
        id_factory=lambda prefix: f"{prefix}_1",
        recover_on_startup=False,
    )
    return artifacts, service


def wait_summary(service, expected, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        summary = service.get_summary("prj_test", "run_1")
        if summary.status == expected:
            return summary
        time.sleep(0.01)
    raise AssertionError(f"run did not reach {expected}")


def wait_cleanup(service, timeout=3.0):
    deadline = time.monotonic() + timeout
    while service._registry.snapshot() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert service._registry.snapshot() == ()


def test_result_pipeline_commits_final_data_and_report_from_memory(tmp_path):
    integrator = PipelineIntegrator()
    rules = PipelineRuleEngine()
    evidence = PipelineEvidenceService()
    renderer = RecordingRenderer()
    _, service = make_runtime(
        tmp_path,
        integrator=integrator,
        rule_engine=rules,
        evidence_service=evidence,
        report_renderer=renderer,
    )
    service.create_run("prj_test", ["mat_1"], None)
    completed = wait_summary(service, RunStatus.COMPLETED)
    wait_cleanup(service)

    assert integrator.calls == rules.calls == evidence.calls == 1
    snapshot, branch_results, findings = integrator.inputs
    assert snapshot.run_id == "run_1"
    assert [item.branch for item in branch_results] == [BranchCode.PDF_IMAGE]
    assert findings == []
    assert rules.rule_snapshot_inputs == [None]
    assert completed.rule_check_summary.status == "skipped"
    timings = {item.phase: item for item in completed.phase_timings if item.scope_type == "phase"}
    assert timings[PhaseCode.INTEGRATION].status == "completed"
    assert timings[PhaseCode.RULE_CHECK].status == "skipped"
    assert timings[PhaseCode.EVIDENCE_INDEX].status == "completed"
    assert service.get_data_list_view("prj_test", "run_1") == [final_row()]

    report = tmp_path / "projects" / "prj_test" / "runs" / "run_1" / "report.md"
    assert report.read_text(encoding="utf-8").startswith("# 测试报告")
    assert len(renderer.calls) == 1
    _, rows, rule_result, tool_findings, evidence_index = renderer.calls[0]
    assert rows == [final_row()]
    assert rule_result.status == "skipped"
    assert tool_findings == []
    assert evidence_index.evidence == {}
    service.shutdown(0)


def test_selected_rule_snapshot_is_retained_and_passed_to_rule_owner(tmp_path):
    rules = SelectedRuleEngine()
    _, service = make_runtime(
        tmp_path,
        integrator=PipelineIntegrator(),
        rule_engine=rules,
        evidence_service=PipelineEvidenceService(),
        register_rule_set=True,
    )
    service.create_run("prj_test", ["mat_1"], "factory_rules")
    completed = wait_summary(service, RunStatus.COMPLETED)
    wait_cleanup(service)

    assert rules.calls == 1
    assert rules.rule_snapshot_inputs[0].rule_set_id == "factory_rules"
    assert completed.rule_check_summary.status == "completed"
    assert completed.rule_check_summary.rule_set_id == "factory_rules"
    assert completed.rule_check_summary.attempted_rule_count == 0
    rule_timing = next(
        item for item in completed.phase_timings if item.phase == PhaseCode.RULE_CHECK
    )
    assert rule_timing.status == "completed"
    service.shutdown(0)


def test_artifact_index_failure_skips_evidence_and_uses_final_list_fallback(tmp_path):
    evidence = PipelineEvidenceService()
    renderer = RecordingRenderer()
    _, service = make_runtime(
        tmp_path,
        integrator=PipelineIntegrator(),
        rule_engine=PipelineRuleEngine(),
        evidence_service=evidence,
        report_renderer=renderer,
        artifact_type=FailDraftIndex,
    )
    service.create_run("prj_test", ["mat_1"], None)
    completed = wait_summary(service, RunStatus.COMPLETED_WITH_ERRORS)
    wait_cleanup(service)

    assert evidence.calls == 0
    assert [item.code for item in completed.error_summary] == ["artifact_index_update_failed"]
    evidence_timing = next(
        item for item in completed.phase_timings if item.phase == PhaseCode.EVIDENCE_INDEX
    )
    assert evidence_timing.status == "skipped"
    assert service.get_data_list_view("prj_test", "run_1") == [final_row()]
    assert renderer.calls[0][2] is None
    assert renderer.calls[0][4] is None
    service.shutdown(0)


def test_evidence_failure_and_report_failure_do_not_undo_terminal_cleanup(tmp_path):
    renderer = RecordingRenderer(fail=True)
    _, service = make_runtime(
        tmp_path,
        integrator=PipelineIntegrator(),
        rule_engine=PipelineRuleEngine(),
        evidence_service=PipelineEvidenceService(fail=True),
        report_renderer=renderer,
    )
    service.create_run("prj_test", ["mat_1"], None)
    completed = wait_summary(service, RunStatus.COMPLETED_WITH_ERRORS)
    wait_cleanup(service)

    assert [item.code for item in completed.error_summary] == ["evidence_index_build_failed"]
    assert service.get_data_list_view("prj_test", "run_1") == [final_row()]
    assert len(renderer.calls) == 1
    report = tmp_path / "projects" / "prj_test" / "runs" / "run_1" / "report.md"
    assert not report.exists()
    assert service.get_summary("prj_test", "run_1").status == RunStatus.COMPLETED_WITH_ERRORS
    service.shutdown(0)


def test_project_deletion_during_report_render_skips_write_and_still_cleans_session(tmp_path):
    entered = Event()
    release = Event()

    class BlockingRenderer(RecordingRenderer):
        def render(self, *args):
            self.calls.append(args)
            entered.set()
            release.wait(2)
            return "# 不应写入\n"

    renderer = BlockingRenderer()
    _, service = make_runtime(
        tmp_path,
        integrator=PipelineIntegrator(),
        rule_engine=PipelineRuleEngine(),
        evidence_service=PipelineEvidenceService(),
        report_renderer=renderer,
    )
    service.create_run("prj_test", ["mat_1"], None)
    wait_summary(service, RunStatus.COMPLETED)
    assert entered.wait(2)
    with service.projects.mutate("prj_test") as mutation:
        mutation.remove(cleanup_mode="retain")
    release.set()
    wait_cleanup(service)

    assert len(renderer.calls) == 1
    assert not (tmp_path / "projects" / "prj_test" / "runs" / "run_1" / "report.md").exists()
    service.shutdown(0)


def test_stop_during_integration_does_not_start_rule_or_write_final_list(tmp_path):
    entered = Event()
    release = Event()

    class BlockingIntegrator(PipelineIntegrator):
        def merge(self, snapshot, branch_results, tool_findings, context):
            entered.set()
            release.wait(2)
            context.stop_signal.check_boundary()
            raise AssertionError("stop boundary should interrupt integration")

    rules = PipelineRuleEngine()
    evidence = PipelineEvidenceService()
    _, service = make_runtime(
        tmp_path,
        integrator=BlockingIntegrator(),
        rule_engine=rules,
        evidence_service=evidence,
        report_renderer=RecordingRenderer(),
    )
    service.create_run("prj_test", ["mat_1"], None)
    assert entered.wait(2)
    stopping = service.request_stop("prj_test", "run_1")
    assert stopping.status == RunStatus.RUNNING
    release.set()
    stopped = wait_summary(service, RunStatus.STOPPED)
    wait_cleanup(service)

    assert stopped.current_phase == PhaseCode.INTEGRATION
    integration_timing = next(
        item for item in stopped.phase_timings if item.phase == PhaseCode.INTEGRATION
    )
    assert integration_timing.status == "stopped"
    assert rules.calls == 0
    assert evidence.calls == 0
    assert not (
        tmp_path / "projects" / "prj_test" / "runs" / "run_1" / "project_data_list.json"
    ).exists()
    service.shutdown(0)
