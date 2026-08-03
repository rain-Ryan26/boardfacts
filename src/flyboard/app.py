from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from flyboard.config import AppConfig
from flyboard.evidence import DeterministicEvidenceService
from flyboard.ingestion import MaterialStager
from flyboard.model_calls import (
    HttpxModelTransport,
    ModelCallClient,
    ModelEndpointConfig,
    load_default_policy_registry,
)
from flyboard.models import build_current_field_table
from flyboard.reports import MarkdownReportRenderer
from flyboard.repositories import ArtifactRepository, ProjectRepository
from flyboard.services import MaterialService, ProjectService, RuleSetService
from flyboard.workflow import RunService

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class Application:
    """The single process composition root; construction is explicit and side-effect scoped."""

    config: AppConfig
    projects: ProjectRepository
    artifacts: ArtifactRepository
    project_service: ProjectService
    material_service: MaterialService
    rule_set_service: RuleSetService
    evidence_service: DeterministicEvidenceService
    run_service: RunService

    def close(self, grace_seconds: float = 5.0) -> None:
        self.run_service.shutdown(grace_seconds)


def build_application(
    config: AppConfig | None = None,
    *,
    recover_on_startup: bool = True,
) -> Application:
    resolved = config or AppConfig.from_env()
    _LOGGER.info("BoardFacts storage root: %s", resolved.storage_root)
    projects = ProjectRepository(resolved.storage_root)
    artifacts = ArtifactRepository(resolved.storage_root)
    rule_sets = RuleSetService(resolved.storage_root)
    project_service = ProjectService(projects, artifacts)
    material_service = MaterialService(
        projects,
        MaterialStager(resolved.storage_root),
        recover_on_startup=recover_on_startup,
    )
    run_service = RunService(
        projects,
        artifacts,
        rule_sets,
        model_client=_model_client_from_env(),
        report_renderer=MarkdownReportRenderer(),
        recover_on_startup=recover_on_startup,
    )
    evidence_service = run_service.evidence_service
    if not isinstance(evidence_service, DeterministicEvidenceService):
        raise TypeError("default composition must use DeterministicEvidenceService")
    return Application(
        config=resolved,
        projects=projects,
        artifacts=artifacts,
        project_service=project_service,
        material_service=material_service,
        rule_set_service=rule_sets,
        evidence_service=evidence_service,
        run_service=run_service,
    )


def _model_client_from_env():
    base = os.environ.get("FLYBOARD_QWEN_API_BASE")
    key = os.environ.get("FLYBOARD_QWEN_API_KEY")
    if base is None and key is None:
        return None
    endpoint = ModelEndpointConfig.from_env()
    return ModelCallClient(
        policies=load_default_policy_registry(),
        transport=HttpxModelTransport(endpoint),
    )


def register_rule_set(application: Application, rule_set_path: str):
    return application.rule_set_service.register_file(rule_set_path, build_current_field_table())
