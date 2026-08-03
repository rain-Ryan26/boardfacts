from __future__ import annotations

from datetime import datetime
from typing import Protocol

from flyboard.errors import ServiceError
from flyboard.models import (
    AccessLevel,
    ArtifactConsumer,
    ArtifactIndexEntry,
    BranchCode,
    GerberJobPreparation,
    LayerDefinition,
    LayerDefinitionItem,
    LayerDefinitionsArtifact,
    PhaseCode,
    RunStatus,
    TrajectoryEvent,
    TrajectoryEventType,
)

from ._state import TERMINAL_BRANCH_STATUSES
from .commit import RunCommitContext


class LayerDefinitionBuilder(Protocol):
    def build_draft(
        self,
        preparation: GerberJobPreparation,
        current_definition: LayerDefinition | None,
        submitted_layers: list[LayerDefinitionItem],
        updated_at: datetime,
    ) -> LayerDefinition: ...

    def build_confirmed(
        self,
        preparation: GerberJobPreparation,
        current_definition: LayerDefinition,
        confirmed_at: datetime,
    ) -> LayerDefinition: ...


class LayerDefinitionCommandsMixin:
    @staticmethod
    def _preparation_for(session, job_material_id: str) -> GerberJobPreparation:
        state = session.preparation_states_by_job.get(job_material_id)
        preparation = (
            state
            if isinstance(state, GerberJobPreparation)
            else getattr(state, "preparation", None)
        )
        if not isinstance(preparation, GerberJobPreparation):
            raise ServiceError("gerber_preparation_not_ready")
        return preparation

    def save_layer_definition_draft(
        self,
        project_id: str,
        run_id: str,
        job_material_id: str,
        submitted_layers: list[LayerDefinitionItem],
    ) -> LayerDefinition:
        if self.layer_definitions is None:
            raise ServiceError("layer_definition_service_unavailable")
        session = self._registry.get(project_id, run_id)
        if session is None:
            raise ServiceError("run_state_conflict")

        def command(context: RunCommitContext) -> LayerDefinition:
            if (
                session.summary.status != RunStatus.RUNNING
                or session.summary.stop_requested_at is not None
                or session.summary.branch_status.gerber in TERMINAL_BRANCH_STATUSES
            ):
                raise ServiceError("run_state_conflict")
            preparation = self._preparation_for(session, job_material_id)
            current = session.definitions_by_job.get(job_material_id)
            if current is not None and current.confirmed_at is not None:
                raise ServiceError("run_state_conflict")
            definition = self.layer_definitions.build_draft(
                preparation, current, submitted_layers, self.clock()
            )
            self._write_definitions(context, session, definition)
            return definition.model_copy(deep=True)

        return session.channel.execute(command)

    def _write_definitions(self, context, session, replacement) -> bool:
        if session.snapshot is None:
            raise ServiceError("run_session_unavailable")
        definitions = dict(session.definitions_by_job)
        definitions[replacement.job_material_id] = replacement
        ordered = [
            definitions[job_id]
            for job_id in session.snapshot.groups.gerber_jobs
            if job_id in definitions
        ]
        artifact = LayerDefinitionsArtifact(
            schema_version="1.0", run_id=session.run_id, definitions=ordered
        )
        entry = ArtifactIndexEntry(
            path="layer_definitions.json",
            kind="layer_definitions",
            producer_phase=PhaseCode.BRANCH_ANALYSIS,
            consumers=[
                ArtifactConsumer.WORKFLOW,
                ArtifactConsumer.API,
                ArtifactConsumer.GUI,
                ArtifactConsumer.REGRESSION,
            ],
            access_level=AccessLevel.PROJECT_SCOPED,
            source_material_ids=[item.job_material_id for item in ordered],
        )
        try:
            session.ops.write_json(
                context,
                artifact,
                entry,
                branch=BranchCode.GERBER,
                on_index_commit=lambda _path: session.definitions_by_job.__setitem__(
                    replacement.job_material_id,
                    replacement,
                ),
            )
        except ServiceError as exc:
            if (
                exc.code != "run_record_write_failed"
                or session.definitions_by_job.get(replacement.job_material_id) is not replacement
            ):
                raise
            return False
        return True

    def _read_confirmed_layer_definition(
        self,
        project_id: str,
        run_id: str,
        job_material_id: str,
    ) -> LayerDefinition:
        with self.projects.guard_product_run(project_id, run_id):
            summary = self.artifacts.read_run_summary(project_id, run_id)
            if summary.artifact_index_path is None:
                raise ServiceError("artifact_index_unavailable")
            index = self.artifacts.read_artifact_index(
                project_id, run_id, summary.artifact_index_path
            )
            definitions = self.artifacts.read_layer_definitions(project_id, run_id, index)
        existing = next(
            (
                item
                for item in definitions
                if item.job_material_id == job_material_id and item.confirmed_at is not None
            ),
            None,
        )
        if existing is None:
            raise ServiceError("run_state_conflict")
        return existing

    def confirm_layer_definition(
        self, project_id: str, run_id: str, job_material_id: str
    ) -> LayerDefinition:
        session = self._registry.get(project_id, run_id)
        if session is None:
            return self._read_confirmed_layer_definition(project_id, run_id, job_material_id)
        if self.layer_definitions is None:
            raise ServiceError("layer_definition_service_unavailable")

        def command(context: RunCommitContext) -> LayerDefinition:
            current = session.definitions_by_job.get(job_material_id)
            if current is not None and current.confirmed_at is not None:
                return current.model_copy(deep=True)
            if (
                session.summary.status != RunStatus.RUNNING
                or session.summary.stop_requested_at is not None
                or session.summary.branch_status.gerber in TERMINAL_BRANCH_STATUSES
                or current is None
            ):
                raise ServiceError("run_state_conflict")
            preparation = self._preparation_for(session, job_material_id)
            confirmed = self.layer_definitions.build_confirmed(preparation, current, self.clock())
            artifact_event_written = self._write_definitions(context, session, confirmed)
            if not artifact_event_written:
                return confirmed.model_copy(deep=True)
            try:
                session.ops.append_trajectory(
                    context,
                    TrajectoryEvent(
                        schema_version="1.0",
                        run_id=run_id,
                        timestamp=self.clock(),
                        phase=PhaseCode.BRANCH_ANALYSIS,
                        branch=BranchCode.GERBER,
                        event_type=TrajectoryEventType.LAYER_DEFINITION_CONFIRMED,
                        summary={"job_material_id": job_material_id},
                        source_material_ids=[job_material_id],
                        artifact_paths=["layer_definitions.json"],
                        error_ids=[],
                    ),
                )
            except ServiceError:
                pass
            else:
                try:
                    self._check_gerber_readiness(context, session)
                except Exception as exc:
                    self._fail_gerber_readiness(context, session, exc)
            return confirmed.model_copy(deep=True)

        with session.lifecycle_lock:
            if session.cleanup_started:
                result = None
            else:
                result = session.channel.execute(command)
        if result is None:
            return self._read_confirmed_layer_definition(project_id, run_id, job_material_id)
        session.control.wake()
        return result
