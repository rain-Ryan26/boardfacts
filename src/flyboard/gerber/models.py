from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

OUTPUT_KEYS = (
    "layers.copper_layer_count",
    "layers.solder_mask_sides",
    "layers.silkscreen_sides",
    "geometry.job_bounds_mm",
    "geometry.min_trace_width_mm",
    "geometry.min_copper_spacing_mm",
    "drill.used_hole_size_summary",
    "drill.used_hole_quantity_summary",
    "drill.slot_size_summary",
    "drill.min_used_hole_diameter_mm",
    "geometry.min_annular_ring_mm",
    "drill.plating_summary.plated",
    "drill.plating_summary.non_plated",
)

OUTPUT_UNITS = {
    "layers.copper_layer_count": None,
    "layers.solder_mask_sides": None,
    "layers.silkscreen_sides": None,
    "geometry.job_bounds_mm": "mm",
    "geometry.min_trace_width_mm": "mm",
    "geometry.min_copper_spacing_mm": "mm",
    "drill.used_hole_size_summary": "mm",
    "drill.used_hole_quantity_summary": None,
    "drill.slot_size_summary": "mm",
    "drill.min_used_hole_diameter_mm": "mm",
    "geometry.min_annular_ring_mm": "mm",
    "drill.plating_summary.plated": None,
    "drill.plating_summary.non_plated": None,
}

FIELD_CODES = {
    "layers.copper_layer_count": "layer_count",
    "layers.solder_mask_sides": "solder_mask_layers",
    "layers.silkscreen_sides": "silkscreen_layers",
    "geometry.job_bounds_mm": "gerber_bounds_mm",
    "geometry.min_trace_width_mm": "min_trace_width_mm",
    "geometry.min_copper_spacing_mm": "min_trace_spacing_mm",
    "drill.used_hole_size_summary": "drill_size_list",
    "drill.used_hole_quantity_summary": "drill_quantity_list",
    "drill.slot_size_summary": "slot_size_list",
    "drill.min_used_hole_diameter_mm": "min_hole_diameter_mm",
    "geometry.min_annular_ring_mm": "min_annular_ring_mm",
    "drill.plating_summary.plated": "plated_hole_requirement",
    "drill.plating_summary.non_plated": "non_plated_hole_requirement",
}


class GerberExpectedOutputMissing(ValueError):
    pass


class GerberModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class GerberOutput(GerberModel):
    key: str = Field(min_length=1)
    applicable: bool
    value: object | None
    unit: Literal["mm"] | None
    coverage: Literal["complete", "partial", "none"]
    source_files: list[str]

    @model_validator(mode="after")
    def validate_output(self) -> GerberOutput:
        if self.key not in OUTPUT_KEYS:
            raise ValueError("unknown Gerber output key")
        if self.unit != OUTPUT_UNITS[self.key]:
            raise ValueError("Gerber output unit does not match its key")
        if len(self.source_files) != len(set(self.source_files)):
            raise ValueError("source_files must be unique")
        if not self.applicable:
            if self.value is not None or self.coverage != "complete":
                raise ValueError("non-applicable output requires null/complete")
            return self
        if self.coverage == "complete" and self.value is None:
            raise ValueError("complete applicable output requires a value")
        if self.coverage == "partial" and self.value is None:
            raise ValueError("partial output requires a candidate value")
        if self.coverage == "none" and self.value is not None:
            raise ValueError("none coverage requires null value")
        _validate_value(self.key, self.value)
        return self


class GerberCheck(GerberModel):
    code: str = Field(min_length=1)
    kind: Literal["engineering_warning", "coverage_gap"]
    message: str = Field(min_length=1)
    affected_output_keys: list[str]
    source_files: list[str]

    @model_validator(mode="after")
    def validate_check(self) -> GerberCheck:
        _validate_diagnostic_keys(self.affected_output_keys)
        _require_unique(self.source_files, "source_files")
        return self


class GerberToolError(GerberModel):
    code: str = Field(min_length=1)
    severity: Literal["warning", "error"]
    message: str = Field(min_length=1)
    affected_output_keys: list[str]
    source_files: list[str]
    artifact_paths: list[str]
    suggested_action: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_error(self) -> GerberToolError:
        _validate_diagnostic_keys(self.affected_output_keys)
        _require_unique(self.source_files, "source_files")
        _require_unique(self.artifact_paths, "artifact_paths")
        return self


class GerberResult(GerberModel):
    schema_version: Literal["1.0"]
    run_id: str = Field(min_length=1)
    job_material_id: str = Field(min_length=1)
    outputs: list[GerberOutput]
    checks: list[GerberCheck]
    errors: list[GerberToolError]

    @model_validator(mode="after")
    def validate_result(self) -> GerberResult:
        keys = [item.key for item in self.outputs]
        if set(keys) != set(OUTPUT_KEYS) or len(keys) != len(OUTPUT_KEYS):
            raise ValueError("GerberResult must contain every output key exactly once")
        coverage_keys = {
            key
            for item in self.checks
            if item.kind == "coverage_gap"
            for key in item.affected_output_keys
        }
        error_keys = {key for item in self.errors for key in item.affected_output_keys}
        engineering_keys = {
            key
            for item in self.checks
            if item.kind == "engineering_warning"
            for key in item.affected_output_keys
        }
        affected = coverage_keys | error_keys | engineering_keys
        for output in self.outputs:
            if not output.applicable and output.key in affected:
                raise ValueError("non-applicable output cannot have a diagnostic")
            if output.coverage in {"partial", "none"} and output.key not in (
                coverage_keys | error_keys
            ):
                raise ValueError("incomplete output requires a coverage or error diagnostic")
            if output.coverage == "complete" and output.key in (coverage_keys | error_keys):
                raise ValueError("complete output cannot have a reliability diagnostic")
        return self


class GerberFieldOutcome(GerberModel):
    output_key: str = Field(min_length=1)
    field_code: str = Field(min_length=1)
    status: Literal["available", "unreliable", "missing"]
    value: str | None
    evidence_ids: list[str]
    tool_finding_ids: list[str]
    error_ids: list[str]

    @model_validator(mode="after")
    def validate_outcome(self) -> GerberFieldOutcome:
        if self.output_key not in OUTPUT_KEYS or FIELD_CODES[self.output_key] != self.field_code:
            raise ValueError("Gerber outcome mapping is invalid")
        if len(self.evidence_ids) != 2 or len(set(self.evidence_ids)) != 2:
            raise ValueError("Gerber outcome requires result and adapter evidence ids")
        _require_unique(self.tool_finding_ids, "tool_finding_ids")
        _require_unique(self.error_ids, "error_ids")
        if self.status == "missing":
            if self.value is not None or not (self.tool_finding_ids or self.error_ids):
                raise ValueError("missing outcome requires null value and diagnostics")
        elif self.value is None:
            raise ValueError("available or unreliable outcome requires a value")
        if self.status == "unreliable" and not (self.tool_finding_ids or self.error_ids):
            raise ValueError("unreliable outcome requires diagnostics")
        return self


class GerberAdapterSummaryArtifact(GerberModel):
    schema_version: Literal["1.0"]
    run_id: str = Field(min_length=1)
    job_material_id: str = Field(min_length=1)
    field_outcomes: list[GerberFieldOutcome]

    @model_validator(mode="after")
    def validate_outcomes(self) -> GerberAdapterSummaryArtifact:
        keys = [item.output_key for item in self.field_outcomes]
        fields = [item.field_code for item in self.field_outcomes]
        _require_unique(keys, "field_outcomes.output_key")
        _require_unique(fields, "field_outcomes.field_code")
        if keys != [key for key in OUTPUT_KEYS if key in set(keys)]:
            raise ValueError("Gerber field outcomes must follow output spec order")
        return self


class GerberDiagnosticRequestArtifact(GerberModel):
    schema_version: Literal["1.0"]
    run_id: str = Field(min_length=1)
    job_material_id: str = Field(min_length=1)
    output_key: str = Field(min_length=1)
    mode: Literal["missing_value", "unreliable_value", "finding_only"]
    model: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    system_prompt: str = Field(min_length=1)
    field_context: dict[str, object]
    output_schema_version: Literal["1.0"]


class GerberDiagnosticResponseArtifact(GerberModel):
    schema_version: Literal["1.0"]
    run_id: str = Field(min_length=1)
    job_material_id: str = Field(min_length=1)
    output_key: str = Field(min_length=1)
    model: str = Field(min_length=1)
    content: str


class GerberDiagnosticSubmission(GerberModel):
    append_check_note: str = Field(min_length=1)

    @field_validator("append_check_note")
    @classmethod
    def normalize_note(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("append_check_note must contain non-whitespace text")
        return normalized


def _validate_diagnostic_keys(values: list[str]) -> None:
    _require_unique(values, "affected_output_keys")
    if any(value not in OUTPUT_KEYS for value in values):
        raise ValueError("diagnostic references an unknown Gerber output key")


def _validate_value(key: str, value: object | None) -> None:
    if value is None:
        return
    scalar_keys = {
        "geometry.min_trace_width_mm",
        "geometry.min_copper_spacing_mm",
        "drill.min_used_hole_diameter_mm",
        "geometry.min_annular_ring_mm",
    }
    if key == "layers.copper_layer_count":
        if type(value) is not int or value < 1:
            raise ValueError("copper layer count must be a positive integer")
    elif key in {"layers.solder_mask_sides", "layers.silkscreen_sides"}:
        if value not in (["TOP"], ["BOT"], ["TOP", "BOT"]):
            raise ValueError("layer side summary is invalid")
    elif key == "geometry.job_bounds_mm":
        if type(value) is not dict or set(value) != {"min_x", "min_y", "max_x", "max_y"}:
            raise ValueError("job bounds shape is invalid")
        numbers = [value[name] for name in ("min_x", "min_y", "max_x", "max_y")]
        if not all(_finite_number(item) for item in numbers):
            raise ValueError("job bounds must be finite")
        if numbers[0] > numbers[2] or numbers[1] > numbers[3]:
            raise ValueError("job bounds must be ordered")
    elif key in scalar_keys:
        if not _finite_number(value) or value < 0:
            raise ValueError("Gerber scalar output must be finite and non-negative")
    elif key == "drill.used_hole_size_summary":
        if (
            type(value) is not list
            or not value
            or not all(_finite_number(item) and item > 0 for item in value)
        ):
            raise ValueError("used hole size summary is invalid")
        if value != sorted(set(value)):
            raise ValueError("used hole sizes must be sorted and unique")
    elif key == "drill.used_hole_quantity_summary":
        _validate_quantity_summary(value)
    elif key == "drill.slot_size_summary":
        _validate_slot_summary(value)
    elif key in {"drill.plating_summary.plated", "drill.plating_summary.non_plated"}:
        if type(value) is not str or not value.strip():
            raise ValueError("plating summary must be a non-empty string")


def _validate_quantity_summary(value: object) -> None:
    if type(value) is not list or not value:
        raise ValueError("quantity summary must be non-empty")
    previous = -1.0
    for item in value:
        if type(item) is not dict or set(item) != {"diameter_mm", "quantity"}:
            raise ValueError("quantity item shape is invalid")
        diameter = item["diameter_mm"]
        quantity = item["quantity"]
        if not _finite_number(diameter) or diameter <= 0 or diameter <= previous:
            raise ValueError("quantity diameters must be positive and ordered")
        if type(quantity) is not int or quantity < 1:
            raise ValueError("quantity must be a positive integer")
        previous = diameter


def _validate_slot_summary(value: object) -> None:
    if type(value) is not list or not value:
        raise ValueError("slot summary must be non-empty")
    previous: tuple[float, float] | None = None
    for item in value:
        if type(item) is not dict or set(item) != {"width_mm", "length_mm", "quantity"}:
            raise ValueError("slot item shape is invalid")
        width = item["width_mm"]
        length = item["length_mm"]
        quantity = item["quantity"]
        if not (_finite_number(width) and width > 0 and _finite_number(length) and length > 0):
            raise ValueError("slot dimensions must be finite and positive")
        current = (width, length)
        if previous is not None and current <= previous:
            raise ValueError("slots must be strictly ordered")
        if type(quantity) is not int or quantity < 1:
            raise ValueError("slot quantity must be positive")
        previous = current


def _finite_number(value: object) -> bool:
    return type(value) in {int, float} and math.isfinite(value)


def _require_unique(values: list[object], name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must be unique")
