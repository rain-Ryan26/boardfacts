from __future__ import annotations

import io
from collections.abc import Callable
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw

from flyboard.errors import ServiceError
from flyboard.models import (
    AccessLevel,
    ArtifactConsumer,
    ArtifactIndex,
    ArtifactIndexEntry,
    BranchResult,
    Evidence,
    EvidenceIndex,
    EvidenceMetadataResponse,
    EvidenceType,
    GerberArtifactEvidenceLocator,
    GerberArtifactEvidenceSeed,
    ImageEvidenceLocator,
    ImageEvidenceSeed,
    IntegrationResult,
    PdfPageEvidenceLocator,
    PdfPageEvidenceSeed,
    PhaseCode,
    Region,
    to_evidence_metadata,
)
from flyboard.repositories import ArtifactRepository

if TYPE_CHECKING:
    from flyboard.workflow import AnalysisContext


class _EvidenceWriteFailed(RuntimeError):
    pass


class DeterministicEvidenceService:
    """Build and read the complete v1 EvidenceIndex without sidecar inference."""

    def __init__(
        self,
        artifacts: ArtifactRepository,
        *,
        highlighter: Callable[[bytes, Region], bytes] | None = None,
    ) -> None:
        self._artifacts = artifacts
        self._highlighter = highlighter or _highlight_png

    def build_index(
        self,
        branch_results: list[BranchResult],
        integration_result: IntegrationResult,
        context: AnalysisContext,
    ) -> EvidenceIndex | None:
        context.stop_signal.check_boundary()
        try:
            artifact_index = self._current_artifact_index(context)
            seeds = [seed for result in branch_results for seed in result.evidence]
            by_id, referenced = _validate_seed_and_reference_sets(seeds, integration_result)
            if not referenced.issubset(by_id):
                raise ValueError("business reference has no EvidenceSeed")
            evidence: dict[str, Evidence] = {}
            locators = {}
            for seed in seeds:
                target_path = _seed_target_path(seed)
                entry = _project_scoped_entry(artifact_index, target_path, seed.source_material_id)
                source_content, media_type = self._artifacts.read_project_scoped_content(
                    context.project_id,
                    context.run_id,
                    entry=entry,
                    expected_path=target_path,
                )
                asset_path = target_path
                if isinstance(seed, (PdfPageEvidenceSeed, ImageEvidenceSeed)):
                    if media_type != "image/png":
                        raise ValueError("PDF/image source asset is not a PNG")
                    if seed.region is not None:
                        source_size = _validate_region(source_content, seed.region)
                        highlighted = self._build_highlight(
                            seed, source_content, source_size, context
                        )
                        if highlighted is None:
                            asset_path = seed.source_asset_path
                        else:
                            asset_path = highlighted
                evidence[seed.evidence_id] = Evidence(
                    evidence_id=seed.evidence_id,
                    type=EvidenceType(seed.type),
                    source_material_id=seed.source_material_id,
                    file_path=seed.file_path,
                    page_index=seed.page_index,
                    asset_path=asset_path,
                    region=seed.region,
                    note=seed.note,
                )
                locators[seed.evidence_id] = _locator(seed)
            result = EvidenceIndex(
                schema_version="1.0",
                run_id=context.run_id,
                evidence=evidence,
                locators=locators,
            )
        except _EvidenceWriteFailed:
            return None
        except (ServiceError, ValueError, OSError):
            self._record_build_failure(context)
            return None

        try:
            context.artifact_writer.write_json(
                result,
                ArtifactIndexEntry(
                    path="evidence_index.json",
                    kind="evidence_index",
                    producer_phase=PhaseCode.EVIDENCE_INDEX,
                    consumers=[
                        ArtifactConsumer.WORKFLOW,
                        ArtifactConsumer.API,
                        ArtifactConsumer.REPORT,
                        ArtifactConsumer.REGRESSION,
                    ],
                    access_level=AccessLevel.PROJECT_SCOPED,
                    source_material_ids=_stable_source_ids(seeds),
                ),
            )
        except ServiceError:
            return None
        return result

    def _current_artifact_index(self, context) -> ArtifactIndex:
        summary = self._artifacts.read_run_summary(context.project_id, context.run_id)
        if summary.artifact_index_path is None:
            raise ServiceError("artifact_index_unavailable")
        return self._artifacts.read_artifact_index(
            context.project_id,
            context.run_id,
            summary.artifact_index_path,
        )

    def _build_highlight(self, seed, source_content, source_size, context):
        try:
            content = self._highlighter(source_content, seed.region)
            _validate_highlight(content, source_size)
        except Exception:
            context.diagnostics.record_error(
                code="evidence_highlight_failed",
                severity="warning",
                phase="evidence_index",
                branch=None,
                message="证据高亮图生成失败，已降级为页图或整图。",
                source_material_ids=[seed.source_material_id],
                artifact_paths=[seed.source_asset_path],
                suggested_action="通过原始页图或整图复核该证据。",
            )
            return None
        path = f"pdf/highlights/{seed.evidence_id}.png"
        try:
            return context.artifact_writer.write_content(
                content,
                "image/png",
                ArtifactIndexEntry(
                    path=path,
                    kind="evidence_highlight",
                    producer_phase=PhaseCode.EVIDENCE_INDEX,
                    consumers=[
                        ArtifactConsumer.API,
                        ArtifactConsumer.REPORT,
                        ArtifactConsumer.REGRESSION,
                    ],
                    access_level=AccessLevel.PROJECT_SCOPED,
                    source_material_ids=[seed.source_material_id],
                ),
                optional_failure_code="evidence_highlight_failed",
            )
        except ServiceError as exc:
            if exc.code == "evidence_highlight_failed":
                return None
            raise _EvidenceWriteFailed from exc

    @staticmethod
    def _record_build_failure(context) -> None:
        context.diagnostics.record_error(
            code="evidence_index_build_failed",
            severity="error",
            phase="evidence_index",
            branch=None,
            message="证据 seed、业务引用或目标 artifact 未通过一致性校验。",
            source_material_ids=[],
            artifact_paths=[],
            suggested_action="检查分支证据引用和已登记的 project-scoped artifact。",
        )

    def get(
        self,
        project_id: str,
        run_id: str,
        evidence_index: EvidenceIndex,
        evidence_id: str,
    ) -> EvidenceMetadataResponse:
        del project_id
        if evidence_index.run_id != run_id:
            raise ServiceError("evidence_index_unavailable")
        evidence = evidence_index.evidence.get(evidence_id)
        if evidence is None:
            raise ServiceError("evidence_not_found")
        return to_evidence_metadata(evidence)

    def get_content(
        self,
        project_id: str,
        run_id: str,
        artifact_index: ArtifactIndex,
        evidence_index: EvidenceIndex,
        evidence_id: str,
    ) -> tuple[bytes, str]:
        if artifact_index.run_id != run_id or evidence_index.run_id != run_id:
            raise ServiceError("evidence_index_unavailable")
        evidence = evidence_index.evidence.get(evidence_id)
        if evidence is None or evidence_id not in evidence_index.locators:
            raise ServiceError("evidence_not_found")
        entries = [
            entry
            for entry in artifact_index.entries
            if entry.path == evidence.asset_path
            and entry.access_level == AccessLevel.PROJECT_SCOPED
        ]
        if len(entries) != 1:
            raise ServiceError("artifact_content_unavailable")
        return self._artifacts.read_project_scoped_content(
            project_id,
            run_id,
            entry=entries[0],
            expected_path=evidence.asset_path,
        )


def _validate_seed_and_reference_sets(seeds, integration_result):
    by_id = {}
    by_key = {}
    for seed in seeds:
        key = _seed_key(seed)
        if seed.evidence_id in by_id or key in by_key:
            raise ValueError("duplicate EvidenceSeed id or key")
        by_id[seed.evidence_id] = seed
        by_key[key] = seed.evidence_id
    rows = (
        integration_result.project_data_list
        if integration_result.project_data_list is not None
        else integration_result.draft_project_data_list
    )
    referenced = {evidence_id for row in rows for evidence_id in row.evidence_ids}
    referenced.update(
        evidence_id
        for row in rows
        for candidate in row.conflict_candidates
        for evidence_id in candidate.evidence_ids
    )
    referenced.update(
        evidence_id
        for finding in integration_result.tool_findings
        for evidence_id in finding.evidence_ids
    )
    return by_id, referenced


def _seed_key(seed):
    region = (
        None
        if seed.region is None
        else (
            seed.region.x,
            seed.region.y,
            seed.region.width,
            seed.region.height,
            seed.region.unit,
        )
    )
    if isinstance(seed, PdfPageEvidenceSeed):
        return (seed.type, seed.source_material_id, seed.page_index, region)
    if isinstance(seed, ImageEvidenceSeed):
        return (
            seed.type,
            seed.source_material_id,
            seed.source_image_index,
            region,
        )
    return (
        seed.type,
        seed.source_material_id,
        seed.artifact_kind,
        seed.artifact_path,
    )


def _seed_target_path(seed):
    if isinstance(seed, (PdfPageEvidenceSeed, ImageEvidenceSeed)):
        return seed.source_asset_path
    if isinstance(seed, GerberArtifactEvidenceSeed):
        return seed.artifact_path
    raise ValueError("unsupported EvidenceSeed type")


def _project_scoped_entry(artifact_index, path, source_material_id):
    entries = [
        entry
        for entry in artifact_index.entries
        if entry.path == path and entry.access_level == AccessLevel.PROJECT_SCOPED
    ]
    if len(entries) != 1 or source_material_id not in entries[0].source_material_ids:
        raise ValueError("Evidence target is not uniquely indexed for its source")
    return entries[0]


def _locator(seed):
    if isinstance(seed, PdfPageEvidenceSeed):
        return PdfPageEvidenceLocator(kind="pdf_page", source_asset_path=seed.source_asset_path)
    if isinstance(seed, ImageEvidenceSeed):
        return ImageEvidenceLocator(
            kind="image",
            source_asset_path=seed.source_asset_path,
            source_image_index=seed.source_image_index,
        )
    if isinstance(seed, GerberArtifactEvidenceSeed):
        return GerberArtifactEvidenceLocator(
            kind="gerber_artifact", artifact_kind=seed.artifact_kind
        )
    raise ValueError("unsupported EvidenceSeed type")


def _validate_region(content: bytes, region: Region) -> tuple[int, int]:
    with Image.open(io.BytesIO(content)) as opened:
        size = opened.size
        opened.verify()
    width, height = size
    if region.x + region.width > width or region.y + region.height > height:
        raise ValueError("EvidenceSeed region is outside its source asset")
    return size


def _validate_highlight(content: bytes, expected_size: tuple[int, int]) -> None:
    with Image.open(io.BytesIO(content)) as opened:
        if opened.format != "PNG" or opened.size != expected_size:
            raise ValueError("highlight must be a same-size PNG")
        opened.verify()


def _highlight_png(content: bytes, region: Region) -> bytes:
    with Image.open(io.BytesIO(content)) as opened:
        image = opened.convert("RGBA")
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    box = (
        region.x,
        region.y,
        region.x + region.width - 1,
        region.y + region.height - 1,
    )
    draw.rectangle(box, fill=(255, 235, 59, 72), outline=(255, 152, 0, 255), width=4)
    highlighted = Image.alpha_composite(image, overlay).convert("RGB")
    stream = io.BytesIO()
    highlighted.save(stream, format="PNG")
    return stream.getvalue()


def _stable_source_ids(seeds):
    values = []
    for seed in seeds:
        if seed.source_material_id not in values:
            values.append(seed.source_material_id)
    return values
