import time

from fastapi.testclient import TestClient

from flyboard.api import create_http_app
from flyboard.app import Application
from flyboard.config import AppConfig
from flyboard.evidence import DeterministicEvidenceService
from flyboard.ingestion import MaterialStager
from flyboard.models import (
    AccessLevel,
    ArtifactConsumer,
    ArtifactIndexEntry,
    BranchCode,
    BranchResult,
    GerberJobPreparation,
    IntegrationResult,
    PhaseCode,
    ProjectDataListArtifact,
    ProjectDataRow,
)
from flyboard.repositories import ArtifactRepository, ProjectRepository
from flyboard.rules import RuleEngine
from flyboard.services import MaterialService, ProjectService, RuleSetService
from flyboard.workflow import RunService


class ApiGerberAnalyzer:
    def prepare(self, snapshot, job_material_id, context):
        preparation = GerberJobPreparation(
            schema_version="1.0",
            project_id=snapshot.project_id,
            run_id=snapshot.run_id,
            job_material_id=job_material_id,
            layer_inputs=[
                {
                    "layer_id": "layer_top",
                    "file_path": "top.gbr",
                    "detected_type": "gerber",
                }
            ],
        )
        context.artifact_writer.write_content(
            b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
            "image/svg+xml",
            ArtifactIndexEntry(
                path=f"gerberscout/{job_material_id}/preview/layer_top.svg",
                kind="gerber_preview",
                producer_phase=PhaseCode.GERBER_PREPARATION,
                consumers=[
                    ArtifactConsumer.API,
                    ArtifactConsumer.GUI,
                    ArtifactConsumer.REGRESSION,
                ],
                access_level=AccessLevel.PROJECT_SCOPED,
                source_material_ids=[job_material_id],
            ),
        )
        context.artifact_writer.write_json(
            preparation,
            ArtifactIndexEntry(
                path=f"gerberscout/{job_material_id}/preparation.json",
                kind="gerber_preparation",
                producer_phase=PhaseCode.GERBER_PREPARATION,
                consumers=[
                    ArtifactConsumer.WORKFLOW,
                    ArtifactConsumer.API,
                    ArtifactConsumer.GUI,
                    ArtifactConsumer.REGRESSION,
                ],
                access_level=AccessLevel.PROJECT_SCOPED,
                source_material_ids=[job_material_id],
            ),
        )
        return preparation, []

    def analyze(self, snapshot, context, preparations, layer_definitions):
        del context, layer_definitions
        return BranchResult(
            schema_version="1.0",
            run_id=snapshot.run_id,
            branch=BranchCode.GERBER,
            status="completed",
            source_material_ids=list(snapshot.groups.gerber_jobs),
            fields=[
                {
                    "field_code": "board_thickness_mm",
                    "field_name": "成品板厚",
                    "field_value": "1.6",
                    "unit": "mm",
                    "category": "板件设计参数",
                    "branch_state": "clear_now",
                    "source": "gerberscout",
                    "source_material_id": preparations[0].job_material_id,
                    "source_ref": "top.gbr",
                    "evidence_ids": [],
                    "tool_finding_ids": [],
                    "error_ids": [],
                    "check_note": "API Gerber 候选。",
                }
            ],
            unknown_notes=[],
            tool_findings=[],
            evidence=[],
            errors=[],
        )


class ApiIntegrator:
    def merge(self, snapshot, branch_results, tool_findings, context):
        del branch_results
        row = ProjectDataRow(
            index=1,
            field_code="board_thickness_mm",
            field_name="成品板厚",
            field_value="1.6",
            source="top.gbr",
            evidence_ids=[],
            tool_finding_ids=[],
            error_ids=[],
            conflict_candidates=[],
            category="板件设计参数",
            field_state="confirmed",
            check_note="API Gerber 候选。",
            check_conclusion="检查通过",
        )
        result = IntegrationResult(
            draft_project_data_list=[row],
            project_data_list=None,
            tool_findings=tool_findings,
            errors=[],
            evidence_index=None,
        )
        context.artifact_writer.write_json(
            ProjectDataListArtifact(
                schema_version="1.0",
                run_id=snapshot.run_id,
                rows=result.draft_project_data_list,
            ),
            ArtifactIndexEntry(
                path="integration/draft_project_data_list.json",
                kind="draft_project_data_list",
                producer_phase=PhaseCode.INTEGRATION,
                consumers=[ArtifactConsumer.WORKFLOW, ArtifactConsumer.REGRESSION],
                access_level=AccessLevel.RESTRICTED_DEBUG,
                source_material_ids=[],
            ),
        )
        return result


def _application(tmp_path):
    config = AppConfig(tmp_path / "storage")
    projects = ProjectRepository(config.storage_root)
    artifacts = ArtifactRepository(config.storage_root)
    rules = RuleSetService(config.storage_root)
    project_service = ProjectService(projects, artifacts)
    material_service = MaterialService(
        projects,
        MaterialStager(config.storage_root),
        recover_on_startup=False,
    )
    run_service = RunService(
        projects,
        artifacts,
        rules,
        gerber_analyzer=ApiGerberAnalyzer(),
        integrator=ApiIntegrator(),
        rule_engine=RuleEngine(),
        evidence_service=DeterministicEvidenceService(artifacts),
        recover_on_startup=False,
    )
    return Application(
        config=config,
        projects=projects,
        artifacts=artifacts,
        project_service=project_service,
        material_service=material_service,
        rule_set_service=rules,
        evidence_service=run_service.evidence_service,
        run_service=run_service,
    )


def test_gerber_preview_draft_confirm_and_read_routes(tmp_path):
    application = _application(tmp_path)
    source = tmp_path / "top.gbr"
    source.write_bytes(b"%FSLAX24Y24*%\nM02*")
    try:
        with TestClient(create_http_app(application)) as client:
            project = client.post("/projects", json={"project_name": "Gerber API"}).json()
            material = client.post(
                f"/projects/{project['project_id']}/materials",
                json={"input_path": str(source)},
            ).json()["materials"][0]
            assert material["kind"] == "gerber_job"
            run = client.post(
                f"/projects/{project['project_id']}/runs",
                json={"material_ids": [material["material_id"]], "rule_set_id": None},
            ).json()
            base = f"/projects/{project['project_id']}/runs/{run['run_id']}"

            deadline = time.monotonic() + 3
            preparations = []
            while not preparations:
                assert time.monotonic() < deadline
                response = client.get(f"{base}/gerber-preparations")
                assert response.status_code == 200
                preparations = response.json()
                time.sleep(0.01)
            preparation = preparations[0]
            preview = client.get(
                f"{base}/gerber-preparations/{material['material_id']}/layers/layer_top/preview"
            )
            assert preview.status_code == 200
            assert preview.headers["content-type"].startswith("image/svg+xml")

            draft_body = {
                "project_id": project["project_id"],
                "run_id": run["run_id"],
                "job_material_id": material["material_id"],
                "layers": [
                    {
                        "layer_id": preparation["layer_inputs"][0]["layer_id"],
                        "file_path": preparation["layer_inputs"][0]["file_path"],
                        "role": "top_copper",
                        "copper_layer_index": 1,
                    }
                ],
            }
            draft = client.put(
                f"{base}/layer-definitions/{material['material_id']}", json=draft_body
            )
            assert draft.status_code == 200
            assert draft.json()["confirmed_at"] is None
            assert client.get(f"{base}/layer-definitions").json() == [draft.json()]

            confirmed = client.post(
                f"{base}/layer-definitions/{material['material_id']}/confirm", json={}
            )
            assert confirmed.status_code == 200
            assert confirmed.json()["confirmed_at"] is not None
            repeated = client.post(
                f"{base}/layer-definitions/{material['material_id']}/confirm", json={}
            )
            assert repeated.status_code == 200
            assert repeated.json() == confirmed.json()

            summary = client.get(base).json()
            while summary["status"] not in {
                "completed",
                "completed_with_errors",
                "failed",
                "stopped",
            }:
                assert time.monotonic() < deadline
                time.sleep(0.01)
                summary = client.get(base).json()
            assert summary["status"] in {"completed", "completed_with_errors"}, summary[
                "error_summary"
            ]
            project_id = project["project_id"]
            run_id = run["run_id"]
            material_id = material["material_id"]
            layer_id = preparation["layer_inputs"][0]["layer_id"]
    finally:
        application.close(0)

    restarted_application = _application(tmp_path)
    try:
        with TestClient(create_http_app(restarted_application)) as client:
            restored_project = next(
                item for item in client.get("/projects").json() if item["project_id"] == project_id
            )
            assert restored_project["latest_run_id"] == run_id
            base = f"/projects/{project_id}/runs/{run_id}"
            assert client.get(base).json()["status"] in {"completed", "completed_with_errors"}
            restored_preparations = client.get(f"{base}/gerber-preparations")
            assert restored_preparations.status_code == 200
            assert restored_preparations.json()[0]["job_material_id"] == material_id
            restored_definitions = client.get(f"{base}/layer-definitions")
            assert restored_definitions.status_code == 200
            assert restored_definitions.json()[0]["confirmed_at"] is not None
            restored_preview = client.get(
                f"{base}/gerber-preparations/{material_id}/layers/{layer_id}/preview"
            )
            assert restored_preview.status_code == 200
            assert restored_preview.headers["content-type"].startswith("image/svg+xml")
    finally:
        restarted_application.close(0)
