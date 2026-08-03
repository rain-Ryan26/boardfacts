from __future__ import annotations

import argparse
import json
import os
import time
from itertools import count
from pathlib import Path

from flyboard.ingestion import MaterialStager
from flyboard.model_calls import (
    HttpxModelTransport,
    ModelCallClient,
    ModelEndpointConfig,
    load_default_policy_registry,
)
from flyboard.models import LayerDefinitionItem, RunStatus, build_current_field_table
from flyboard.reports import MarkdownReportRenderer
from flyboard.repositories import ArtifactRepository, ProjectRepository
from flyboard.services import MaterialService, ProjectService, RuleSetService, ServiceError
from flyboard.workflow import RunService

PROJECT_ID = "prj_s7_synthetic"
RUN_ID = "run_s7_synthetic"


def main() -> int:
    repository_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Run the first-version S7 chain with non-confidential synthetic inputs."
    )
    parser.add_argument(
        "--sources",
        type=Path,
        default=repository_root / "testsources/synthetic_pcb/materials",
    )
    parser.add_argument(
        "--rule-set",
        type=Path,
        default=repository_root / "testsources/synthetic_pcb/rules/PCB_MANUFACTURING_RULES_R3.json",
    )
    parser.add_argument("--env-file", type=Path, default=repository_root / ".env")
    parser.add_argument("--storage-root", type=Path, required=True)
    args = parser.parse_args()

    sources = args.sources.resolve(strict=True)
    rule_path = args.rule_set.resolve(strict=True)
    storage_root = args.storage_root.resolve()
    storage_root.mkdir(parents=True, exist_ok=False)
    _load_endpoint_env(args.env_file)

    projects = ProjectRepository(storage_root)
    artifacts = ArtifactRepository(storage_root)
    project_service = ProjectService(
        projects,
        artifacts,
        project_id_factory=lambda: PROJECT_ID,
    )
    project_service.create_project("S7 synthetic product check")
    material_numbers = count(1)
    materials = MaterialService(
        projects,
        MaterialStager(storage_root),
        request_id_factory=lambda: "req_s7_synthetic",
        material_id_factory=lambda: f"mat_s7_{next(material_numbers):04d}",
        recover_on_startup=False,
    ).register_materials(PROJECT_ID, str(sources))
    if materials.failures:
        raise RuntimeError(f"synthetic material registration failed: {materials.failures!r}")

    rule_sets = RuleSetService(storage_root)
    rule_validation = rule_sets.register_file(str(rule_path), build_current_field_table())
    if rule_validation.validation_status != "valid" or rule_validation.rule_set_summary is None:
        raise RuntimeError(f"synthetic rule set is invalid: {rule_validation.errors!r}")

    counters: dict[str, int] = {}

    def make_id(prefix: str) -> str:
        if prefix == "run":
            return RUN_ID
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}_s7_{counters[prefix]:04d}"

    model = ModelCallClient(
        policies=load_default_policy_registry(),
        transport=HttpxModelTransport(ModelEndpointConfig.from_env()),
    )
    service = RunService(
        projects,
        artifacts,
        rule_sets,
        model_client=model,
        report_renderer=MarkdownReportRenderer(),
        id_factory=make_id,
        recover_on_startup=False,
    )
    try:
        material_ids = [item.material_id for item in materials.materials]
        gerber_ids = [
            item.material_id for item in materials.materials if item.kind.value == "gerber_job"
        ]
        created = service.create_run(
            PROJECT_ID,
            material_ids,
            rule_validation.rule_set_summary.rule_set_id,
        )
        for preparation in _wait_preparations(
            service,
            artifacts,
            created.run_id,
            expected_count=len(gerber_ids),
        ):
            layers = [_layer_definition(item) for item in preparation.layer_inputs]
            service.save_layer_definition_draft(
                PROJECT_ID,
                created.run_id,
                preparation.job_material_id,
                layers,
            )
            service.confirm_layer_definition(
                PROJECT_ID,
                created.run_id,
                preparation.job_material_id,
            )

        summary = _wait_terminal(service, created.run_id)
        data_view = service.get_data_list_view(PROJECT_ID, created.run_id)
        findings = service.get_tool_findings_view(PROJECT_ID, created.run_id)
        result = {
            "project_id": PROJECT_ID,
            "run_id": created.run_id,
            "status": summary.status,
            "branch_status": summary.branch_status.model_dump(mode="json"),
            "material_count": len(material_ids),
            "data_row_count": len(data_view) if isinstance(data_view, list) else None,
            "tool_finding_count": len(findings),
            "rule_check_summary": summary.rule_check_summary.model_dump(mode="json"),
            "model_calls": [item.model_dump(mode="json") for item in summary.model_call_summaries],
            "errors": [item.model_dump(mode="json") for item in summary.error_summary],
            "report_exists": (
                storage_root / f"projects/{PROJECT_ID}/runs/{created.run_id}/report.md"
            ).is_file(),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if summary.status in {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_ERRORS} else 1
    finally:
        service.shutdown(5)


def _layer_definition(layer_input) -> LayerDefinitionItem:
    name = Path(layer_input.file_path).name.lower()
    if layer_input.detected_type.value == "drill":
        role, index = "drill", None
    elif "top_copper" in name:
        role, index = "top_copper", 1
    elif "bottom_copper" in name:
        role, index = "bottom_copper", 2
    elif "outline" in name:
        role, index = "board_outline", None
    else:
        role, index = "unknown", None
    return LayerDefinitionItem(
        layer_id=layer_input.layer_id,
        file_path=layer_input.file_path,
        role=role,
        copper_layer_index=index,
    )


def _wait_preparations(
    service: RunService,
    artifacts: ArtifactRepository,
    run_id: str,
    *,
    expected_count: int,
    timeout: float = 60.0,
):
    if expected_count == 0:
        return []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        summary = service.get_summary(PROJECT_ID, run_id)
        try:
            stored = artifacts.read_run_summary(PROJECT_ID, run_id)
            if stored.artifact_index_path is None:
                raise ServiceError("artifact_index_unavailable")
            index = artifacts.read_artifact_index(PROJECT_ID, run_id, stored.artifact_index_path)
            values = artifacts.read_gerber_preparations(PROJECT_ID, run_id, index)
        except ServiceError:
            values = []
        if len(values) == expected_count:
            return values
        if summary.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
            raise RuntimeError("run became terminal before all Gerber preparations were readable")
        time.sleep(0.05)
    raise TimeoutError("synthetic Gerber preparations did not become readable")


def _wait_terminal(service: RunService, run_id: str, timeout: float = 900.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        summary = service.get_summary(PROJECT_ID, run_id)
        if summary.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
            return summary
        time.sleep(0.1)
    raise TimeoutError("synthetic S7 run did not reach a terminal state")


def _load_endpoint_env(path: Path) -> None:
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            if key in {"FLYBOARD_QWEN_API_BASE", "FLYBOARD_QWEN_API_KEY"}:
                os.environ.setdefault(key, value.strip().strip('"').strip("'"))


if __name__ == "__main__":
    raise SystemExit(main())
