from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from flyboard.models import FieldTable


class IntegrationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class EvidenceSummaryInput(IntegrationModel):
    description: str = Field(min_length=1)
    note: str | None


class CandidateInput(IntegrationModel):
    candidate_index: int = Field(ge=0)
    field_value: str = Field(min_length=1)
    unit: str | None
    branch_state: Literal["clear_now", "unknown", "conflict"]
    source_label: str = Field(min_length=1)
    check_note: str
    evidence_summaries: list[EvidenceSummaryInput]


class DiagnosticInput(IntegrationModel):
    diagnostic_index: int = Field(ge=0)
    source_label: str = Field(min_length=1)
    branch_state: Literal["missing"]
    check_note: str
    evidence_summaries: list[EvidenceSummaryInput]


class ToolFindingSummaryInput(IntegrationModel):
    message: str = Field(min_length=1)


class ErrorSummaryInput(IntegrationModel):
    message: str = Field(min_length=1)
    suggested_action: str | None


class DiagnosticsInput(IntegrationModel):
    ordered_inputs: list[DiagnosticInput]
    tool_findings: list[ToolFindingSummaryInput]
    errors: list[ErrorSummaryInput]


class FieldTask(IntegrationModel):
    field_code: str = Field(min_length=1)
    operation: Literal["compare", "summarize", "diagnostic"]
    decision_rule: str = Field(min_length=1)
    fixed_primary_candidate: Literal[0] | None
    ordered_candidates: list[CandidateInput]
    diagnostics: DiagnosticsInput

    @model_validator(mode="after")
    def validate_task_shape(self) -> FieldTask:
        candidate_indexes = [item.candidate_index for item in self.ordered_candidates]
        if candidate_indexes != list(range(len(candidate_indexes))):
            raise ValueError("candidate indexes must be contiguous and ordered")
        diagnostic_indexes = [item.diagnostic_index for item in self.diagnostics.ordered_inputs]
        if diagnostic_indexes != list(range(len(diagnostic_indexes))):
            raise ValueError("diagnostic indexes must be contiguous and ordered")
        if self.operation == "diagnostic":
            if (
                self.fixed_primary_candidate is not None
                or self.ordered_candidates
                or not self.diagnostics.ordered_inputs
            ):
                raise ValueError("diagnostic task requires only ordered diagnostic inputs")
        elif self.fixed_primary_candidate != 0 or not self.ordered_candidates:
            raise ValueError("compare and summarize tasks require fixed primary candidate zero")
        return self


class ModelRequestArtifact(IntegrationModel):
    schema_version: Literal["1.0"]
    run_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    batch_id: str = Field(min_length=1)
    operation: Literal["compare", "summarize", "diagnostic"]
    system_prompt: str = Field(min_length=1)
    field_table: FieldTable
    tasks: list[FieldTask] = Field(min_length=1)
    output_schema_version: Literal["1.0"]

    @model_validator(mode="after")
    def validate_batch(self) -> ModelRequestArtifact:
        if any(task.operation != self.operation for task in self.tasks):
            raise ValueError("all batch tasks must use the request operation")
        _require_unique(self.tasks, key=lambda item: item.field_code)
        return self


class ModelUsageArtifact(IntegrationModel):
    input_tokens: int | None = Field(ge=0)
    output_tokens: int | None = Field(ge=0)


class ModelResponseArtifact(IntegrationModel):
    schema_version: Literal["1.0"]
    run_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    content: str
    usage: ModelUsageArtifact | None


class SubmittedItem(IntegrationModel):
    field_code: str = Field(min_length=1)
    field_value: str | None
    candidate_relation: Literal["equivalent", "different", "uncertain"] | None
    suggested_field_state: Literal["confirmed", "unknown", "conflict", "missing"]
    append_check_note: str = Field(min_length=1)
    check_conclusion: str = Field(min_length=1)


class ItemRejection(IntegrationModel):
    field_code: str | None
    reason_code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ModelParsedArtifact(IntegrationModel):
    schema_version: Literal["1.0"]
    run_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    status: Literal["accepted", "accepted_with_rejections", "rejected"]
    accepted_items: list[SubmittedItem]
    rejections: list[ItemRejection]
    fallback_field_codes: list[str]
    error_ids: list[str]


def _require_unique(values, *, key=lambda item: item) -> None:
    identities = [key(item) for item in values]
    if len(identities) != len(set(identities)):
        raise ValueError("integration task references must be unique")
