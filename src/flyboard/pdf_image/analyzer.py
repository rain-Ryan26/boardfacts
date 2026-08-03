from __future__ import annotations

from typing import TYPE_CHECKING

from flyboard.errors import ServiceError
from flyboard.models import (
    AccessLevel,
    ArtifactConsumer,
    ArtifactIndexEntry,
    BranchCode,
    BranchResult,
    BranchResultStatus,
    PhaseCode,
    RunInputSnapshot,
)
from flyboard.repositories import ProjectRepository

from .batching import PdfBatchRunner, PdfImageSettings
from .materialize import collect_drafts, materialize_drafts
from .models import PdfAssetManifest
from .preparation import PdfAssetPreparer

if TYPE_CHECKING:
    from flyboard.workflow import AnalysisContext


class PdfImageAnalyzer:
    """Production PDF/image preparation, model protocol, and EvidenceSeed owner."""

    def __init__(
        self,
        projects: ProjectRepository,
        *,
        preparer: PdfAssetPreparer | None = None,
        batch_runner: PdfBatchRunner | None = None,
        settings: PdfImageSettings | None = None,
    ) -> None:
        selected_settings = settings or PdfImageSettings()
        self._preparer = preparer or PdfAssetPreparer(projects)
        self._batch_runner = batch_runner or PdfBatchRunner(settings=selected_settings)

    def analyze(
        self,
        snapshot: RunInputSnapshot,
        context: AnalysisContext,
    ) -> BranchResult:
        source_ids = _source_ids(snapshot)
        prepared = self._preparer.prepare(snapshot, context)
        errors = list(prepared.errors)
        manifest = PdfAssetManifest(
            schema_version="1.0",
            run_id=context.run_id,
            records=list(prepared.records),
        )
        manifest_path, manifest_error, manifest_exists = _write_json(
            context,
            manifest,
            ArtifactIndexEntry(
                path="pdf/asset_manifest.json",
                kind="pdf_asset_manifest",
                producer_phase=PhaseCode.BRANCH_ANALYSIS,
                consumers=[
                    ArtifactConsumer.WORKFLOW,
                    ArtifactConsumer.DEBUG,
                    ArtifactConsumer.REGRESSION,
                ],
                access_level=AccessLevel.RESTRICTED_DEBUG,
                source_material_ids=source_ids,
            ),
        )
        errors = _merge_errors(errors, [manifest_error])
        if not manifest_exists or manifest_path is None:
            return _failed(context.run_id, source_ids, errors)

        analyzed = self._batch_runner.analyze(snapshot, prepared.assets, context)
        errors = _merge_errors(errors, analyzed.errors)
        trace_error = analyzed.trace.write()
        errors = _merge_errors(errors, [trace_error])
        assets_by_id = {
            item.record.asset_id: item
            for item in prepared.assets
            if item.record.asset_id is not None
        }
        drafts = collect_drafts(analyzed.accepted_assets, assets_by_id)
        fields, unknown_notes, seeds = materialize_drafts(drafts, snapshot, context)
        if not fields and not unknown_notes:
            error = context.diagnostics.record_error(
                code="pdf_image_no_adoptable_result",
                severity="error",
                phase="branch_analysis",
                branch="pdf_image",
                message="PDF/图片分支没有形成可采纳字段或未知说明。",
                source_material_ids=source_ids,
                artifact_paths=[manifest_path],
                suggested_action="检查资料内容、模型服务和分支调试产物。",
            )
            errors = _merge_errors(errors, [error])
            return _failed(context.run_id, source_ids, errors)
        return BranchResult(
            schema_version="1.0",
            run_id=context.run_id,
            branch=BranchCode.PDF_IMAGE,
            status=(
                BranchResultStatus.COMPLETED_WITH_WARNINGS
                if errors
                else BranchResultStatus.COMPLETED
            ),
            source_material_ids=source_ids,
            fields=fields,
            unknown_notes=unknown_notes,
            tool_findings=[],
            evidence=seeds,
            errors=errors,
        )


def _source_ids(snapshot):
    selected = set(snapshot.groups.pdf_inputs + snapshot.groups.image_inputs)
    return [item.material_id for item in snapshot.materials if item.material_id in selected]


def _write_json(context, artifact, entry):
    try:
        return context.artifact_writer.write_json(artifact, entry), None, True
    except ServiceError as exc:
        if exc.error_id is None:
            raise
        error = context.diagnostics.resolve_error(exc.error_id)
        if exc.code == "artifact_index_update_failed":
            return entry.path, error, True
        return None, error, False


def _failed(run_id, source_ids, errors):
    return BranchResult(
        schema_version="1.0",
        run_id=run_id,
        branch=BranchCode.PDF_IMAGE,
        status=BranchResultStatus.FAILED,
        source_material_ids=source_ids,
        fields=[],
        unknown_notes=[],
        tool_findings=[],
        evidence=[],
        errors=errors,
    )


def _merge_errors(existing, added):
    values = list(existing)
    seen = {item.error_id for item in values}
    for item in added:
        if item is not None and item.error_id not in seen:
            seen.add(item.error_id)
            values.append(item)
    return values
