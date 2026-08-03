from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from flyboard.models import FieldTable, ProjectDataRow, Rule, RuleInstruction


class RuleArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RuleOutputProtocol(RuleArtifactModel):
    decision: Literal["violation | no_violation | insufficient_information"]
    append_check_note: Literal["non-empty string"]
    check_conclusion: Literal[
        "non-empty string for violation/insufficient_information; null otherwise"
    ]


class RuleModelRequestArtifact(RuleArtifactModel):
    schema_version: Literal["1.0"]
    run_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    sequence: int = Field(gt=0)
    rule: Rule
    system_prompt: str = Field(min_length=1)
    field_table: FieldTable
    project_data_list: list[ProjectDataRow]
    output_protocol: RuleOutputProtocol


class RuleModelUsageArtifact(RuleArtifactModel):
    input_tokens: int | None = Field(ge=0)
    output_tokens: int | None = Field(ge=0)


class RuleModelResponseArtifact(RuleArtifactModel):
    schema_version: Literal["1.0"]
    run_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    sequence: int = Field(gt=0)
    rule_id: str = Field(min_length=1)
    content: str
    usage: RuleModelUsageArtifact | None


class RuleModelParsedArtifact(RuleArtifactModel):
    schema_version: Literal["1.0"]
    run_id: str = Field(min_length=1)
    sequence: int = Field(gt=0)
    rule_id: str = Field(min_length=1)
    status: Literal["accepted", "rejected"]
    instruction: RuleInstruction | None
    rejection_code: str | None
    rejection_message: str | None
    error_ids: list[str]

    @model_validator(mode="after")
    def validate_result(self) -> RuleModelParsedArtifact:
        if self.status == "accepted":
            if (
                self.instruction is None
                or self.rejection_code is not None
                or self.rejection_message is not None
                or self.error_ids
            ):
                raise ValueError("accepted parsed rule requires only an instruction")
        elif (
            self.instruction is not None
            or self.rejection_code is None
            or self.rejection_message is None
            or len(self.error_ids) != 1
        ):
            raise ValueError("rejected parsed rule requires one rejection and one error")
        return self
