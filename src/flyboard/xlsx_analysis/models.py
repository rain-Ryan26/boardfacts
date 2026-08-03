from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from flyboard.models import FieldTable


class XlsxModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class XlsxSheetRecord(XlsxModel):
    sheet_index: int = Field(ge=1)
    name: str = Field(min_length=1)
    visibility: Literal["visible", "hidden", "veryHidden"]
    outcome: Literal["read", "failed"]
    used_range: str | None
    merged_ranges: list[str]
    hidden_rows: list[int]
    hidden_columns: list[int]
    formula_count: int | None = Field(ge=0)
    serialized_cell_count: int | None = Field(ge=0)
    tool_finding_ids: list[str]
    error_ids: list[str]

    @model_validator(mode="after")
    def validate_outcome(self) -> XlsxSheetRecord:
        if self.hidden_rows != sorted(set(self.hidden_rows)) or any(
            item < 1 for item in self.hidden_rows
        ):
            raise ValueError("hidden_rows must contain sorted unique positive integers")
        if self.hidden_columns != sorted(set(self.hidden_columns)) or any(
            item < 1 for item in self.hidden_columns
        ):
            raise ValueError("hidden_columns must contain sorted unique positive integers")
        if len(self.merged_ranges) != len(set(self.merged_ranges)):
            raise ValueError("merged_ranges must be unique")
        if len(self.tool_finding_ids) != len(set(self.tool_finding_ids)):
            raise ValueError("tool_finding_ids must be unique")
        if len(self.error_ids) != len(set(self.error_ids)):
            raise ValueError("error_ids must be unique")
        if self.outcome == "failed":
            if (
                self.used_range is not None
                or self.merged_ranges
                or self.hidden_rows
                or self.hidden_columns
                or self.formula_count is not None
                or self.serialized_cell_count is not None
                or not self.error_ids
            ):
                raise ValueError("failed sheet must contain only identity and error ids")
        elif self.formula_count is None or self.serialized_cell_count is None:
            raise ValueError("read sheet requires formula and cell counts")
        return self


class XlsxWorkbookRecord(XlsxModel):
    source_material_id: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    outcome: Literal["read", "failed"]
    date_system: Literal["1900", "1904"] | None
    sheets: list[XlsxSheetRecord]
    error_ids: list[str]

    @model_validator(mode="after")
    def validate_outcome(self) -> XlsxWorkbookRecord:
        if len(self.error_ids) != len(set(self.error_ids)):
            raise ValueError("workbook error_ids must be unique")
        if self.outcome == "failed":
            if self.date_system is not None or self.sheets or not self.error_ids:
                raise ValueError("failed workbook requires only error ids")
        elif self.date_system is None:
            raise ValueError("read workbook requires date_system")
        expected = list(range(1, len(self.sheets) + 1))
        if [sheet.sheet_index for sheet in self.sheets] != expected:
            raise ValueError("sheet indexes must be contiguous and ordered")
        return self


class XlsxManifestArtifact(XlsxModel):
    schema_version: Literal["1.0"]
    run_id: str = Field(min_length=1)
    workbooks: list[XlsxWorkbookRecord]


class XlsxCellRecord(XlsxModel):
    source_material_id: str = Field(min_length=1)
    sheet_index: int = Field(ge=1)
    sheet_name: str = Field(min_length=1)
    address: str = Field(pattern=r"^[A-Z]+[1-9][0-9]*$")
    row: int = Field(ge=1)
    column: int = Field(ge=1)
    value_kind: Literal[
        "text",
        "boolean",
        "number",
        "date",
        "datetime",
        "time",
        "percentage",
        "error",
        "empty",
        "unavailable",
    ]
    raw_value: str | None
    normalized_value: str | bool | None
    display_text: str | None
    formula: str | None
    value_origin: Literal["literal", "formula_cache", "formula_without_cache"]
    number_format_code: str | None
    merged_range: str | None
    row_hidden: bool
    column_hidden: bool

    @model_validator(mode="after")
    def validate_origin(self) -> XlsxCellRecord:
        if self.value_origin == "literal" and self.formula is not None:
            raise ValueError("literal cell cannot contain formula")
        if self.value_origin == "formula_cache" and not self.formula:
            raise ValueError("formula cache requires formula")
        if self.value_origin == "formula_without_cache":
            if not self.formula or self.value_kind != "unavailable":
                raise ValueError("formula without cache requires unavailable formula cell")
        return self


class XlsxRawCellsArtifact(XlsxModel):
    schema_version: Literal["1.0"]
    run_id: str = Field(min_length=1)
    cells: list[XlsxCellRecord]

    @model_validator(mode="after")
    def validate_order(self) -> XlsxRawCellsArtifact:
        identities = [
            (cell.source_material_id, cell.sheet_index, cell.row, cell.column)
            for cell in self.cells
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("xlsx cells must be unique")
        return self


class XlsxModelInput(XlsxModel):
    workbooks: list[XlsxWorkbookRecord]
    cells: list[XlsxCellRecord]


class XlsxModelRequestArtifact(XlsxModel):
    schema_version: Literal["1.0"]
    run_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    system_prompt: str = Field(min_length=1)
    analysis_instruction: str = Field(min_length=1)
    field_table: FieldTable
    xlsx_input: XlsxModelInput
    output_schema_version: Literal["1.0"]


class XlsxModelUsage(XlsxModel):
    input_tokens: int | None = Field(ge=0)
    output_tokens: int | None = Field(ge=0)


class XlsxModelResponseArtifact(XlsxModel):
    schema_version: Literal["1.0"]
    run_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    content: str
    usage: XlsxModelUsage | None


class XlsxSubmittedField(XlsxModel):
    field_code: str = Field(min_length=1)
    field_value: str = Field(min_length=1)
    unit: str | None
    branch_state: Literal["clear_now", "unknown", "conflict"]
    source_material_id: str = Field(min_length=1)
    sheet_index: int = Field(ge=1)
    source_cells: list[str] = Field(min_length=1)
    check_note: str


class XlsxSubmittedUnknownNote(XlsxModel):
    field_value: str = Field(min_length=1)
    source_material_id: str = Field(min_length=1)
    sheet_index: int = Field(ge=1)
    source_cells: list[str] = Field(min_length=1)
    check_note: str


class XlsxSubmission(XlsxModel):
    fields: list[object]
    unknown_notes: list[object]


class XlsxRejection(XlsxModel):
    level: Literal["envelope", "field", "unknown_note"]
    item_index: int | None = Field(ge=0)
    reason_code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class XlsxModelParsedArtifact(XlsxModel):
    schema_version: Literal["1.0"]
    run_id: str = Field(min_length=1)
    status: Literal["accepted", "accepted_with_rejections", "rejected"]
    accepted_fields: list[XlsxSubmittedField]
    accepted_unknown_notes: list[XlsxSubmittedUnknownNote]
    rejections: list[XlsxRejection]
    error_ids: list[str]

    @model_validator(mode="after")
    def validate_status(self) -> XlsxModelParsedArtifact:
        accepted = bool(self.accepted_fields or self.accepted_unknown_notes)
        if self.status == "accepted" and (not accepted or self.rejections):
            raise ValueError("accepted parsed result requires items without rejections")
        if self.status == "accepted_with_rejections" and (not accepted or not self.rejections):
            raise ValueError("accepted_with_rejections requires items and rejections")
        if self.status == "rejected" and (accepted or not self.rejections):
            raise ValueError("rejected parsed result requires only rejections")
        if len(self.error_ids) != len(set(self.error_ids)):
            raise ValueError("parsed error_ids must be unique")
        return self
