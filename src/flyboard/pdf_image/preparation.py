from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import fitz
from PIL import Image, ImageOps

from flyboard.errors import ServiceError
from flyboard.models import (
    AccessLevel,
    ArtifactConsumer,
    ArtifactIndexEntry,
    ErrorRecord,
    MaterialKind,
    PhaseCode,
    RunInputMaterial,
    RunInputSnapshot,
)
from flyboard.repositories import ProjectRepository

from .models import PdfAssetRecord, PdfPageTextArtifact

if TYPE_CHECKING:
    from flyboard.workflow import AnalysisContext


@dataclass(frozen=True, slots=True)
class PreparedPdfAsset:
    record: PdfAssetRecord
    content: bytes
    page_text: PdfPageTextArtifact | None


@dataclass(frozen=True, slots=True)
class PdfPreparationResult:
    records: tuple[PdfAssetRecord, ...]
    assets: tuple[PreparedPdfAsset, ...]
    errors: tuple[ErrorRecord, ...]


class PdfAssetPreparer:
    def __init__(
        self,
        projects: ProjectRepository,
        *,
        pdf_dpi: int = 144,
        image_dpi: int = 96,
    ) -> None:
        if pdf_dpi < 1 or image_dpi < 1:
            raise ValueError("asset DPI must be positive")
        self._projects = projects
        self._pdf_dpi = pdf_dpi
        self._image_dpi = image_dpi

    def prepare(
        self,
        snapshot: RunInputSnapshot,
        context: AnalysisContext,
    ) -> PdfPreparationResult:
        selected = set(snapshot.groups.pdf_inputs + snapshot.groups.image_inputs)
        materials = [item for item in snapshot.materials if item.material_id in selected]
        image_order = {
            material_id: index
            for index, material_id in enumerate(snapshot.groups.image_inputs, start=1)
        }
        records: list[PdfAssetRecord] = []
        assets: list[PreparedPdfAsset] = []
        errors: list[ErrorRecord] = []
        next_asset_index = 1
        for material in materials:
            context.stop_signal.check_boundary()
            try:
                source_path = self._projects.resolve_material_source(snapshot.project_id, material)
            except ServiceError:
                error = self._source_error(material, context)
                errors.append(error)
                continue
            if material.kind == MaterialKind.PDF:
                source_records, source_assets, source_errors = self._prepare_pdf(
                    source_path,
                    material,
                    context,
                    next_asset_index=next_asset_index,
                )
            elif material.kind == MaterialKind.IMAGE:
                source_records, source_assets, source_errors = self._prepare_image(
                    source_path,
                    material,
                    image_order[material.material_id],
                    context,
                    next_asset_index=next_asset_index,
                )
            else:
                raise ValueError("PDF asset preparer received an unsupported material kind")
            records.extend(source_records)
            assets.extend(source_assets)
            errors.extend(source_errors)
            next_asset_index += len(source_assets)
        return PdfPreparationResult(tuple(records), tuple(assets), tuple(_unique_errors(errors)))

    def _source_error(self, material, context):
        is_pdf = material.kind == MaterialKind.PDF
        return context.diagnostics.record_error(
            code="pdf_open_failed" if is_pdf else "image_decode_failed",
            severity="error",
            phase="branch_analysis",
            branch="pdf_image",
            message=("PDF 的受管来源无法打开。" if is_pdf else "图片的受管来源无法解码。"),
            source_material_ids=[material.material_id],
            artifact_paths=[],
            suggested_action="重新登记该资料后创建新的运行。",
        )

    def _prepare_pdf(
        self,
        source_path: Path,
        material: RunInputMaterial,
        context: AnalysisContext,
        *,
        next_asset_index: int,
    ):
        document = None
        try:
            document = fitz.open(source_path)
            page_count = document.page_count
            if page_count < 1:
                raise ValueError("PDF has no pages")
        except Exception:
            if document is not None:
                document.close()
            error = context.diagnostics.record_error(
                code="pdf_open_failed",
                severity="error",
                phase="branch_analysis",
                branch="pdf_image",
                message="PDF 无法打开或没有可遍历页面。",
                source_material_ids=[material.material_id],
                artifact_paths=[],
                suggested_action="检查 PDF 内容并重新登记资料。",
            )
            return [], [], [error]

        records: list[PdfAssetRecord] = []
        assets: list[PreparedPdfAsset] = []
        errors: list[ErrorRecord] = []
        try:
            for page_number in range(1, page_count + 1):
                context.stop_signal.check_boundary()
                asset_id = _asset_id("pdf", material.material_id, page_number)
                try:
                    page = document.load_page(page_number - 1)
                    pixmap = page.get_pixmap(dpi=self._pdf_dpi, alpha=False)
                    content = pixmap.tobytes("png")
                    width, height = pixmap.width, pixmap.height
                except Exception:
                    error = context.diagnostics.record_error(
                        code="pdf_page_render_failed",
                        severity="error",
                        phase="branch_analysis",
                        branch="pdf_image",
                        message=f"PDF 第 {page_number} 页未能生成标准 PNG。",
                        source_material_ids=[material.material_id],
                        artifact_paths=[],
                        suggested_action="检查损坏页面或重新导出 PDF。",
                    )
                    errors.append(error)
                    records.append(
                        _failed_record(
                            material,
                            source_kind="pdf",
                            page_number=page_number,
                            image_index=None,
                            text_status="not_attempted",
                            error_ids=[error.error_id],
                        )
                    )
                    continue

                asset_path = f"pdf/assets/{asset_id}.png"
                asset_entry = _asset_entry(asset_path, material.material_id)
                path, write_error, exists = _write_content(context, content, asset_entry)
                if write_error is not None:
                    errors.append(write_error)
                if not exists or path is None:
                    records.append(
                        _failed_record(
                            material,
                            source_kind="pdf",
                            page_number=page_number,
                            image_index=None,
                            text_status="not_attempted",
                            error_ids=[write_error.error_id] if write_error else [],
                        )
                    )
                    continue

                text_artifact, text_status, text_path, text_errors = self._extract_page_text(
                    page,
                    asset_id,
                    material.material_id,
                    context,
                )
                errors.extend(text_errors)
                record = PdfAssetRecord(
                    source_material_id=material.material_id,
                    source_ref=material.source_ref,
                    source_kind="pdf",
                    source_page_number=page_number,
                    source_image_index=None,
                    outcome="prepared",
                    asset_id=asset_id,
                    asset_path=asset_path,
                    mime_type="image/png",
                    sha256=hashlib.sha256(content).hexdigest(),
                    asset_index=next_asset_index + len(assets),
                    width_px=width,
                    height_px=height,
                    dpi=self._pdf_dpi,
                    text_status=text_status,
                    text_artifact_path=text_path,
                    error_ids=[item.error_id for item in ([write_error] if write_error else [])]
                    + [item.error_id for item in text_errors],
                )
                records.append(record)
                assets.append(PreparedPdfAsset(record, content, text_artifact))
        finally:
            document.close()
        return records, assets, _unique_errors(errors)

    def _extract_page_text(self, page, asset_id, material_id, context):
        try:
            original = page.get_text("text")
            normalized = _normalize_page_text(original)
            artifact = PdfPageTextArtifact(
                schema_version="1.0",
                run_id=context.run_id,
                asset_id=asset_id,
                status="extracted" if normalized else "empty",
                text=normalized,
                original_char_count=len(original),
                normalized_char_count=len(normalized),
            )
        except Exception:
            error = context.diagnostics.record_error(
                code="pdf_page_text_extract_failed",
                severity="error",
                phase="branch_analysis",
                branch="pdf_image",
                message="PDF 页面文本提取失败，页面图片仍继续分析。",
                source_material_ids=[material_id],
                artifact_paths=[],
                suggested_action="使用页面图片复核，并检查 PDF 文本对象。",
            )
            return None, "error", None, [error]

        path = f"pdf/page_text/{asset_id}.json"
        entry = ArtifactIndexEntry(
            path=path,
            kind="pdf_page_text",
            producer_phase=PhaseCode.BRANCH_ANALYSIS,
            consumers=[
                ArtifactConsumer.WORKFLOW,
                ArtifactConsumer.DEBUG,
                ArtifactConsumer.REGRESSION,
            ],
            access_level=AccessLevel.RESTRICTED_DEBUG,
            source_material_ids=[material_id],
        )
        written_path, error, exists = _write_json(context, artifact, entry)
        if not exists or written_path is None:
            return None, "error", None, [error] if error is not None else []
        return artifact, artifact.status, path, [error] if error is not None else []

    def _prepare_image(
        self,
        source_path: Path,
        material: RunInputMaterial,
        source_image_index: int,
        context: AnalysisContext,
        *,
        next_asset_index: int,
    ):
        try:
            with Image.open(source_path) as opened:
                image = ImageOps.exif_transpose(opened)
                image.load()
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                stream = io.BytesIO()
                image.save(
                    stream,
                    format="PNG",
                    dpi=(self._image_dpi, self._image_dpi),
                )
                content = stream.getvalue()
                width, height = image.size
        except Exception:
            error = context.diagnostics.record_error(
                code="image_decode_failed",
                severity="error",
                phase="branch_analysis",
                branch="pdf_image",
                message="已登记图片在运行期无法解码。",
                source_material_ids=[material.material_id],
                artifact_paths=[],
                suggested_action="重新导出 PNG/JPEG 并登记为新资料。",
            )
            return (
                [
                    _failed_record(
                        material,
                        source_kind="image",
                        page_number=None,
                        image_index=source_image_index,
                        text_status="not_applicable",
                        error_ids=[error.error_id],
                    )
                ],
                [],
                [error],
            )

        asset_id = _asset_id("image", material.material_id, source_image_index)
        asset_path = f"pdf/assets/{asset_id}.png"
        path, error, exists = _write_content(
            context, content, _asset_entry(asset_path, material.material_id)
        )
        if not exists or path is None:
            return (
                [
                    _failed_record(
                        material,
                        source_kind="image",
                        page_number=None,
                        image_index=source_image_index,
                        text_status="not_applicable",
                        error_ids=[error.error_id] if error else [],
                    )
                ],
                [],
                [error] if error else [],
            )
        record = PdfAssetRecord(
            source_material_id=material.material_id,
            source_ref=material.source_ref,
            source_kind="image",
            source_page_number=None,
            source_image_index=source_image_index,
            outcome="prepared",
            asset_id=asset_id,
            asset_path=asset_path,
            mime_type="image/png",
            sha256=hashlib.sha256(content).hexdigest(),
            asset_index=next_asset_index,
            width_px=width,
            height_px=height,
            dpi=self._image_dpi,
            text_status="not_applicable",
            text_artifact_path=None,
            error_ids=[error.error_id] if error else [],
        )
        return [record], [PreparedPdfAsset(record, content, None)], [error] if error else []


def _asset_id(kind: str, material_id: str, source_index: int) -> str:
    digest = hashlib.sha256(f"{kind}:{material_id}:{source_index}".encode()).hexdigest()[:20]
    return f"asset_{digest}"


def _failed_record(material, *, source_kind, page_number, image_index, text_status, error_ids):
    return PdfAssetRecord(
        source_material_id=material.material_id,
        source_ref=material.source_ref,
        source_kind=source_kind,
        source_page_number=page_number,
        source_image_index=image_index,
        outcome="failed",
        asset_id=None,
        asset_path=None,
        mime_type=None,
        sha256=None,
        asset_index=None,
        width_px=None,
        height_px=None,
        dpi=None,
        text_status=text_status,
        text_artifact_path=None,
        error_ids=error_ids,
    )


def _normalize_page_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = "".join(character for character in value if character in "\n\t" or ord(character) >= 32)
    lines = [re.sub(r"[\t \f\v]+", " ", line).strip() for line in value.split("\n")]
    return "\n".join(lines).strip()


def _asset_entry(path: str, material_id: str) -> ArtifactIndexEntry:
    return ArtifactIndexEntry(
        path=path,
        kind="pdf_asset",
        producer_phase=PhaseCode.BRANCH_ANALYSIS,
        consumers=[ArtifactConsumer.WORKFLOW, ArtifactConsumer.API, ArtifactConsumer.REGRESSION],
        access_level=AccessLevel.PROJECT_SCOPED,
        source_material_ids=[material_id],
    )


def _write_content(context, content, entry):
    try:
        return context.artifact_writer.write_content(content, "image/png", entry), None, True
    except ServiceError as exc:
        if exc.error_id is None:
            raise
        error = context.diagnostics.resolve_error(exc.error_id)
        return (
            (entry.path, error, True)
            if exc.code == "artifact_index_update_failed"
            else (None, error, False)
        )


def _write_json(context, artifact, entry):
    try:
        return context.artifact_writer.write_json(artifact, entry), None, True
    except ServiceError as exc:
        if exc.error_id is None:
            raise
        error = context.diagnostics.resolve_error(exc.error_id)
        return (
            (entry.path, error, True)
            if exc.code == "artifact_index_update_failed"
            else (None, error, False)
        )


def _unique_errors(errors):
    result = []
    seen = set()
    for error in errors:
        if error is not None and error.error_id not in seen:
            seen.add(error.error_id)
            result.append(error)
    return result
