from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from flyboard.models import FieldTable, Region


class PdfImageModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PdfAssetRecord(PdfImageModel):
    source_material_id: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    source_kind: Literal["pdf", "image"]
    source_page_number: int | None = Field(default=None, ge=1)
    source_image_index: int | None = Field(default=None, ge=1)
    outcome: Literal["prepared", "failed"]
    asset_id: str | None = None
    asset_path: str | None = None
    mime_type: Literal["image/png"] | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    asset_index: int | None = Field(default=None, ge=1)
    width_px: int | None = Field(default=None, ge=1)
    height_px: int | None = Field(default=None, ge=1)
    dpi: int | None = Field(default=None, ge=1)
    text_status: Literal["not_applicable", "not_attempted", "extracted", "empty", "error"]
    text_artifact_path: str | None = None
    error_ids: list[str]

    @model_validator(mode="after")
    def validate_record(self) -> PdfAssetRecord:
        if len(self.error_ids) != len(set(self.error_ids)):
            raise ValueError("error_ids must be unique")
        if self.source_kind == "pdf":
            if self.source_page_number is None or self.source_image_index is not None:
                raise ValueError("pdf record requires only source_page_number")
            if self.text_status == "not_applicable":
                raise ValueError("pdf record cannot use not_applicable text status")
        elif self.source_image_index is None or self.source_page_number is not None:
            raise ValueError("image record requires only source_image_index")
        elif self.text_status != "not_applicable":
            raise ValueError("image record text status must be not_applicable")

        prepared = (
            self.asset_id,
            self.asset_path,
            self.mime_type,
            self.sha256,
            self.asset_index,
            self.width_px,
            self.height_px,
            self.dpi,
        )
        if self.outcome == "prepared":
            if any(value is None for value in prepared):
                raise ValueError("prepared record requires complete asset metadata")
        elif any(value is not None for value in prepared):
            raise ValueError("failed record cannot contain asset metadata")

        if self.text_status in {"extracted", "empty"}:
            if self.text_artifact_path is None:
                raise ValueError("successful text extraction requires artifact path")
        elif self.text_artifact_path is not None:
            raise ValueError("other text states cannot contain artifact path")
        return self


class PdfAssetManifest(PdfImageModel):
    schema_version: Literal["1.0"]
    run_id: str = Field(min_length=1)
    records: list[PdfAssetRecord]


class PdfPageTextArtifact(PdfImageModel):
    schema_version: Literal["1.0"]
    run_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    status: Literal["extracted", "empty"]
    text: str
    original_char_count: int = Field(ge=0)
    normalized_char_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_text(self) -> PdfPageTextArtifact:
        if self.normalized_char_count != len(self.text):
            raise ValueError("normalized_char_count must match text")
        if self.status == "empty" and (self.text or self.normalized_char_count != 0):
            raise ValueError("empty page text must contain no text")
        if self.status == "extracted" and not self.text:
            raise ValueError("extracted page text must not be empty")
        return self


class PdfIncludedRange(PdfImageModel):
    start: int = Field(ge=0)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_range(self) -> PdfIncludedRange:
        if self.end <= self.start:
            raise ValueError("included range must have positive length")
        return self


class PdfRequestPageText(PdfImageModel):
    status: Literal["unavailable", "empty", "included"]
    text: str
    original_char_count: int = Field(ge=0)
    normalized_char_count: int = Field(ge=0)
    truncated: bool
    included_ranges: list[PdfIncludedRange]

    @model_validator(mode="after")
    def validate_page_text(self) -> PdfRequestPageText:
        if self.status in {"unavailable", "empty"}:
            if self.text or self.truncated or self.included_ranges:
                raise ValueError("unavailable or empty text cannot contain ranges")
        elif not self.text or not self.included_ranges:
            raise ValueError("included text requires content and ranges")
        for previous, current in zip(self.included_ranges, self.included_ranges[1:]):
            if current.start < previous.end:
                raise ValueError("included ranges must not overlap")
        return self


class PdfRequestAsset(PdfImageModel):
    asset_id: str = Field(min_length=1)
    asset_path: str = Field(min_length=1)
    mime_type: Literal["image/png"]
    page_text: PdfRequestPageText


class PdfModelRequestArtifact(PdfImageModel):
    schema_version: Literal["1.0"]
    run_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    parent_batch_id: str | None
    attempt: int = Field(ge=1)
    source_material_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    system_prompt: str = Field(min_length=1)
    analysis_instruction: str = Field(min_length=1)
    assets: list[PdfRequestAsset] = Field(min_length=1)
    field_table: FieldTable
    output_schema_version: Literal["1.0"]


class PdfModelUsage(PdfImageModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class PdfModelResponseArtifact(PdfImageModel):
    schema_version: Literal["1.0"]
    run_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    model: str = Field(min_length=1)
    content: str
    usage: PdfModelUsage | None


class PdfAcceptedField(PdfImageModel):
    field_code: str = Field(min_length=1)
    field_value: str = Field(min_length=1)
    unit: str | None
    branch_state: Literal["clear_now", "unknown", "conflict"]
    source_text: str | None
    check_note: str = Field(min_length=1)
    region: Region | None


class PdfAcceptedUnknownNote(PdfImageModel):
    text: str = Field(min_length=1)
    source_text: str | None
    check_note: str = Field(min_length=1)
    region: Region | None


class PdfAcceptedAsset(PdfImageModel):
    asset_id: str = Field(min_length=1)
    fields: list[PdfAcceptedField]
    unknown_notes: list[PdfAcceptedUnknownNote]


class PdfModelRejection(PdfImageModel):
    level: Literal["envelope", "asset", "field", "unknown_note"]
    asset_id: str | None
    item_index: int | None = Field(default=None, ge=0)
    reason_code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class PdfRawRegion(PdfImageModel):
    bbox_2d: list[float] = Field(min_length=4, max_length=4)


class PdfRegionDegradation(PdfImageModel):
    asset_id: str = Field(min_length=1)
    item_kind: Literal["field", "unknown_note"]
    item_index: int = Field(ge=0)
    original_region: PdfRawRegion
    normalized_region: Region | None
    reason_code: Literal["clipped", "empty_after_clip"]


class PdfModelParsedArtifact(PdfImageModel):
    schema_version: Literal["1.0"]
    run_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    status: Literal["accepted", "accepted_with_rejections", "rejected"]
    accepted_assets: list[PdfAcceptedAsset]
    rejections: list[PdfModelRejection]
    region_degradations: list[PdfRegionDegradation]
    error_ids: list[str]

    @model_validator(mode="after")
    def validate_status(self) -> PdfModelParsedArtifact:
        accepted = bool(self.accepted_assets)
        rejected = bool(self.rejections)
        expected = (
            "accepted_with_rejections"
            if accepted and rejected
            else "accepted"
            if accepted
            else "rejected"
        )
        if self.status != expected:
            raise ValueError("parsed status does not match accepted/rejected content")
        if len(self.error_ids) != len(set(self.error_ids)):
            raise ValueError("error_ids must be unique")
        return self


class PdfBatchSourceSpan(PdfImageModel):
    kind: Literal["pdf_pages", "image"]
    first_index: int = Field(ge=1)
    last_index: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_span(self) -> PdfBatchSourceSpan:
        if self.last_index < self.first_index:
            raise ValueError("source span must be ordered")
        return self


class PdfBatchCapacity(PdfImageModel):
    max_image_count: int = Field(ge=1)
    request_budget_units: int = Field(ge=1)
    estimated_request_units: int = Field(ge=0)
    page_text_char_limit: int = Field(ge=0)


class PdfBatchTraceEvent(PdfImageModel):
    schema_version: Literal["1.0"]
    run_id: str = Field(min_length=1)
    timestamp: datetime
    event_type: Literal[
        "batch_planned",
        "request_started",
        "provider_limit_rejected",
        "batch_split",
        "retry_started",
        "response_received",
        "protocol_failed",
        "batch_completed",
        "batch_failed",
    ]
    batch_id: str = Field(min_length=1)
    parent_batch_id: str | None
    source_material_id: str = Field(min_length=1)
    asset_ids: list[str] = Field(min_length=1)
    source_span: PdfBatchSourceSpan
    attempt: int = Field(ge=1)
    capacity: PdfBatchCapacity
    outcome: Literal["planned", "running", "split", "retrying", "received", "completed", "failed"]
    artifact_paths: list[str]
    child_batch_ids: list[str]
    error_ids: list[str]
