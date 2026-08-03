from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = "1.0"
SchemaVersion: TypeAlias = Literal["1.0"]


class ContractModel(BaseModel):
    """Base for every closed v1 contract object."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        allow_inf_nan=False,
    )


class FrozenContractModel(ContractModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        allow_inf_nan=False,
        frozen=True,
    )


def _non_empty(value: str) -> str:
    if not value.strip():
        raise ValueError("value must not be empty")
    return value


def _sha256(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("value must be a lowercase SHA-256 digest")
    return value


def _error_id(value: str) -> str:
    if (
        re.fullmatch(
            r"err_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            value,
        )
        is None
    ):
        raise ValueError("error_id must use err_ plus a canonical lowercase UUID")
    return value


def _finding_id(value: str) -> str:
    if (
        re.fullmatch(
            r"finding_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            value,
        )
        is None
    ):
        raise ValueError("finding_id must use finding_ plus a canonical lowercase UUID")
    return value


def _evidence_id(value: str) -> str:
    if re.fullmatch(r"ev_(pdf|image|gerber)_[0-9]{3,}", value) is None:
        raise ValueError("evidence_id must use the canonical type prefix and numeric suffix")
    return value


def _parse_timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif type(value) is str:
        text = value
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("timestamp must be RFC 3339") from exc
    else:
        raise TypeError("timestamp must be an RFC 3339 string or datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include an offset")
    if parsed.microsecond % 1000:
        raise ValueError("timestamp precision must not be finer than milliseconds")
    return parsed.astimezone(timezone.utc)


def _serialize_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _relative_locator(value: str, *, owner: str) -> str:
    if not value:
        raise ValueError(f"{owner} locator must not be empty")
    if "\\" in value:
        raise ValueError(f"{owner} locator must use '/' separators")
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise ValueError(f"{owner} locator must be relative")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value) or "://" in value:
        raise ValueError(f"{owner} locator must not be a URI")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{owner} locator must be normalized")
    return value


def _project_source_ref(value: str) -> str:
    return _relative_locator(value, owner="project source")


def _validate_material_source_ref(
    material_id: str,
    kind: MaterialKind,
    source_ref: str,
    *,
    display_name: str | None = None,
) -> None:
    parts = source_ref.split("/")
    if len(parts) < 2 or parts[:2] != ["sources", material_id]:
        raise ValueError("material source_ref must be rooted at sources/{material_id}")
    if kind == MaterialKind.GERBER_JOB:
        if len(parts) != 2:
            raise ValueError("gerber_job source_ref must identify its managed source directory")
    elif len(parts) < 3:
        raise ValueError("file material source_ref must identify a managed source file")
    if display_name is not None and kind != MaterialKind.GERBER_JOB:
        if parts[-1] != display_name:
            raise ValueError("file material source_ref must end with display_name")


def _run_artifact_path(value: str) -> str:
    return _relative_locator(value, owner="run artifact")


def _gerber_member_path(value: str) -> str:
    return _relative_locator(value, owner="Gerber job member")


def _system_rule_path(value: str) -> str:
    return _relative_locator(value, owner="system rule-set")


def _input_ref(value: str) -> str:
    if value == ".":
        return value
    return _relative_locator(value, owner="registration input")


def _as_tuple(value: object) -> object:
    if isinstance(value, list):
        return tuple(value)
    return value


def _ensure_unique(values: list[str] | tuple[str, ...], *, field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")


def _duration_ms(started_at: datetime, finished_at: datetime) -> int:
    return max(0, math.floor((finished_at - started_at).total_seconds() * 1000))


NonEmptyStr: TypeAlias = Annotated[StrictStr, AfterValidator(_non_empty)]
Sha256: TypeAlias = Annotated[StrictStr, AfterValidator(_sha256)]
ErrorId: TypeAlias = Annotated[StrictStr, AfterValidator(_error_id)]
FindingId: TypeAlias = Annotated[StrictStr, AfterValidator(_finding_id)]
EvidenceId: TypeAlias = Annotated[StrictStr, AfterValidator(_evidence_id)]
ProjectSourceRef: TypeAlias = Annotated[StrictStr, AfterValidator(_project_source_ref)]
RunArtifactPath: TypeAlias = Annotated[StrictStr, AfterValidator(_run_artifact_path)]
GerberMemberPath: TypeAlias = Annotated[StrictStr, AfterValidator(_gerber_member_path)]
SystemRulePath: TypeAlias = Annotated[StrictStr, AfterValidator(_system_rule_path)]
InputRef: TypeAlias = Annotated[StrictStr, AfterValidator(_input_ref)]
AwareTimestamp: TypeAlias = Annotated[
    datetime,
    BeforeValidator(_parse_timestamp),
    PlainSerializer(_serialize_timestamp, return_type=str),
]
PositiveInt: TypeAlias = Annotated[StrictInt, Field(gt=0)]
NonNegativeInt: TypeAlias = Annotated[StrictInt, Field(ge=0)]
HttpStatus: TypeAlias = Annotated[StrictInt, Field(ge=400, le=599)]
FiniteJsonScalar: TypeAlias = StrictStr | StrictInt | StrictFloat | StrictBool | None


class MaterialKind(StrEnum):
    PDF = "pdf"
    IMAGE = "image"
    SPREADSHEET = "spreadsheet"
    GERBER_JOB = "gerber_job"
    UNSUPPORTED = "unsupported"


class MemberKind(StrEnum):
    GERBER = "gerber"
    DRILL = "drill"
    GTD = "gtd"


class UnsupportedReasonCode(StrEnum):
    UNSUPPORTED_FILE_FORMAT = "unsupported_file_format"
    IMAGE_FORMAT_MISMATCH = "image_format_mismatch"
    IMAGE_DECODE_FAILED = "image_decode_failed"
    PDF_INVALID = "pdf_invalid"
    XLSX_INVALID = "xlsx_invalid"
    DESIGN_FILE_UNRECOGNIZED = "design_file_unrecognized"
    GERBER_JOB_AMBIGUOUS = "gerber_job_ambiguous"


class RegistrationFailureReason(StrEnum):
    SOURCE_UNREADABLE = "source_unreadable"
    SOURCE_CHANGED_DURING_REGISTRATION = "source_changed_during_registration"
    SOURCE_COPY_FAILED = "source_copy_failed"
    CLASSIFICATION_FAILED = "classification_failed"


class FieldCategory(StrEnum):
    PROJECT = "项目参数"
    BOARD = "板件设计参数"


class ProducerScope(StrEnum):
    BRANCH_OR_INTEGRATION = "branch_or_integration"
    INTEGRATION_ONLY = "integration_only"


class BranchCode(StrEnum):
    PDF_IMAGE = "pdf_image"
    XLSX = "xlsx"
    GERBER = "gerber"


class CallPurpose(StrEnum):
    PDF_IMAGE = "pdf_image"
    XLSX = "xlsx"
    GERBER_DIAGNOSTIC = "gerber_diagnostic"
    INTEGRATION = "integration"
    RULE_CHECK = "rule_check"


class BranchResultStatus(StrEnum):
    SKIPPED = "skipped"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"


class BranchStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    STOPPED = "stopped"
    SKIPPED = "skipped"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"


class BranchFieldState(StrEnum):
    CLEAR_NOW = "clear_now"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"
    MISSING = "missing"


class FieldState(StrEnum):
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"
    MISSING = "missing"


class EvidenceType(StrEnum):
    PDF_PAGE = "pdf_page"
    IMAGE = "image"
    GERBER_ARTIFACT = "gerber_artifact"


class GerberArtifactKind(StrEnum):
    RESULT = "result"
    ADAPTER_SUMMARY = "adapter_summary"


class ToolFindingKind(StrEnum):
    ENGINEERING_WARNING = "engineering_warning"
    COVERAGE_GAP = "coverage_gap"


class Severity(StrEnum):
    WARNING = "warning"
    ERROR = "error"


class RuleDecision(StrEnum):
    VIOLATION = "violation"
    NO_VIOLATION = "no_violation"
    INSUFFICIENT_INFORMATION = "insufficient_information"


class RuleCheckResultStatus(StrEnum):
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    INTERRUPTED = "interrupted"
    SKIPPED = "skipped"


class RuleCheckStatus(StrEnum):
    SKIPPED = "skipped"
    NOT_REACHED = "not_reached"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    INTERRUPTED = "interrupted"


class PhaseCode(StrEnum):
    INPUT_SNAPSHOT = "input_snapshot"
    GERBER_PREPARATION = "gerber_preparation"
    BRANCH_ANALYSIS = "branch_analysis"
    INTEGRATION = "integration"
    RULE_CHECK = "rule_check"
    EVIDENCE_INDEX = "evidence_index"


class ScopeType(StrEnum):
    PHASE = "phase"
    BRANCH = "branch"
    GERBER_JOB = "gerber_job"


class PhaseTimingStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    SKIPPED = "skipped"
    FAILED = "failed"
    STOPPED = "stopped"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    STOPPED = "stopped"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"


class TrajectoryEventType(StrEnum):
    PHASE_STARTED = "phase_started"
    PHASE_FINISHED = "phase_finished"
    PHASE_SKIPPED = "phase_skipped"
    PHASE_FAILED = "phase_failed"
    RUN_FAILED = "run_failed"
    LAYER_DEFINITION_CONFIRMED = "layer_definition_confirmed"
    STOP_REQUESTED = "stop_requested"
    RUN_STOPPED = "run_stopped"
    MODEL_REQUEST_STARTED = "model_request_started"
    MODEL_RESPONSE_RECEIVED = "model_response_received"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_FINISHED = "tool_call_finished"
    ARTIFACT_WRITTEN = "artifact_written"


class ArtifactConsumer(StrEnum):
    WORKFLOW = "workflow"
    API = "api"
    GUI = "gui"
    CLI = "cli"
    REPORT = "report"
    DEBUG = "debug"
    REGRESSION = "regression"


class AccessLevel(StrEnum):
    PROJECT_SCOPED = "project_scoped"
    RESTRICTED_DEBUG = "restricted_debug"


class LayerRole(StrEnum):
    TOP_COPPER = "top_copper"
    INNER_COPPER = "inner_copper"
    BOTTOM_COPPER = "bottom_copper"
    TOP_SOLDER_MASK = "top_solder_mask"
    BOTTOM_SOLDER_MASK = "bottom_solder_mask"
    TOP_SILKSCREEN = "top_silkscreen"
    BOTTOM_SILKSCREEN = "bottom_silkscreen"
    TOP_PASTE = "top_paste"
    BOTTOM_PASTE = "bottom_paste"
    BOARD_OUTLINE = "board_outline"
    MECHANICAL = "mechanical"
    DRILL = "drill"
    ROUTE = "route"
    UNKNOWN = "unknown"
    IGNORED = "ignored"


class DetectedLayerType(StrEnum):
    GERBER = "gerber"
    DRILL = "drill"


class FieldDefinition(FrozenContractModel):
    field_code: NonEmptyStr
    field_name: NonEmptyStr
    category: FieldCategory
    definition: NonEmptyStr
    unit: NonEmptyStr | None
    is_core: StrictBool
    producer_scope: ProducerScope


FieldEntry = FieldDefinition


def compute_field_table_sha256(
    schema_version: str,
    fields: list[FieldDefinition] | tuple[FieldDefinition, ...],
) -> str:
    payload = json.dumps(
        {
            "schema_version": schema_version,
            "fields": [field.model_dump(mode="json") for field in fields],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class FieldTable(FrozenContractModel):
    schema_version: SchemaVersion
    content_sha256: Sha256
    fields: Annotated[tuple[FieldDefinition, ...], BeforeValidator(_as_tuple)]

    @model_validator(mode="after")
    def validate_catalog(self) -> FieldTable:
        codes = [field.field_code for field in self.fields]
        _ensure_unique(codes, field_name="fields.field_code")
        templates = [field for field in self.fields if "#" in field.field_code]
        if len(templates) != 1 or templates[0].field_code != "unknown_note_###":
            raise ValueError("FieldTable must contain exactly one unknown_note_### template")
        template = templates[0]
        if template.producer_scope != ProducerScope.INTEGRATION_ONLY or template.is_core:
            raise ValueError("unknown_note_### must be integration-only and non-core")
        if any(
            field.is_core and field.producer_scope == ProducerScope.INTEGRATION_ONLY
            for field in self.fields
        ):
            raise ValueError("core fields cannot be integration-only")
        expected = compute_field_table_sha256(self.schema_version, self.fields)
        if self.content_sha256 != expected:
            raise ValueError("content_sha256 does not match the canonical FieldTable content")
        return self


class ProjectMaterial(ContractModel):
    material_id: NonEmptyStr
    kind: MaterialKind
    source_ref: ProjectSourceRef
    display_name: NonEmptyStr
    registered_order: PositiveInt
    registered_at: AwareTimestamp
    member_kinds: list[MemberKind] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    unsupported_reason_code: UnsupportedReasonCode | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def validate_kind_fields(self) -> ProjectMaterial:
        _validate_material_source_ref(
            self.material_id,
            self.kind,
            self.source_ref,
            display_name=self.display_name,
        )
        if self.kind == MaterialKind.GERBER_JOB:
            if not self.member_kinds:
                raise ValueError("gerber_job requires non-empty member_kinds")
            expected = [kind for kind in MemberKind if kind in self.member_kinds]
            if self.member_kinds != expected:
                raise ValueError("member_kinds must be unique and ordered gerber, drill, gtd")
            if self.unsupported_reason_code is not None:
                raise ValueError("gerber_job cannot have unsupported_reason_code")
        else:
            if "member_kinds" in self.model_fields_set:
                raise ValueError("member_kinds must be omitted unless kind is gerber_job")
            if self.kind == MaterialKind.UNSUPPORTED:
                if self.unsupported_reason_code is None:
                    raise ValueError("unsupported material requires unsupported_reason_code")
            elif self.unsupported_reason_code is not None:
                raise ValueError("unsupported_reason_code is only valid for unsupported material")
        return self


class ProjectRecord(ContractModel):
    project_id: NonEmptyStr
    project_name: NonEmptyStr
    created_at: AwareTimestamp
    updated_at: AwareTimestamp
    materials: list[ProjectMaterial]
    latest_run_id: NonEmptyStr | None

    @model_validator(mode="after")
    def validate_record(self) -> ProjectRecord:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot be earlier than created_at")
        ids = [material.material_id for material in self.materials]
        orders = [material.registered_order for material in self.materials]
        _ensure_unique(ids, field_name="materials.material_id")
        _ensure_unique([str(order) for order in orders], field_name="materials.registered_order")
        if orders != sorted(orders):
            raise ValueError("materials must be ordered by registered_order")
        return self


class ProjectSummary(ContractModel):
    project_id: NonEmptyStr
    project_name: NonEmptyStr
    material_count: NonNegativeInt
    latest_run_id: NonEmptyStr | None
    created_at: AwareTimestamp
    updated_at: AwareTimestamp

    @model_validator(mode="after")
    def validate_times(self) -> ProjectSummary:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot be earlier than created_at")
        return self


class MaterialRegistrationFailure(ContractModel):
    input_ref: InputRef
    reason_code: RegistrationFailureReason


class MaterialRegistrationResult(ContractModel):
    materials: list[ProjectMaterial]
    failures: list[MaterialRegistrationFailure]

    @model_validator(mode="after")
    def validate_result(self) -> MaterialRegistrationResult:
        _ensure_unique(
            [material.material_id for material in self.materials],
            field_name="materials.material_id",
        )
        _ensure_unique(
            [failure.input_ref for failure in self.failures],
            field_name="failures.input_ref",
        )
        return self


class ArtifactCleanupResult(ContractModel):
    status: Literal["not_requested", "completed", "pending"]


class ProjectDeletionResult(ContractModel):
    project_id: NonEmptyStr
    status: Literal["deleted"]
    artifact_cleanup: ArtifactCleanupResult


class RunInputMaterial(ContractModel):
    material_id: NonEmptyStr
    material_order: PositiveInt
    kind: MaterialKind
    source_ref: ProjectSourceRef
    sha256: Sha256
    registered_at: AwareTimestamp
    member_kinds: list[MemberKind] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    unsupported_reason_code: UnsupportedReasonCode | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def validate_kind_fields(self) -> RunInputMaterial:
        _validate_material_source_ref(self.material_id, self.kind, self.source_ref)
        if self.kind == MaterialKind.GERBER_JOB:
            if not self.member_kinds:
                raise ValueError("gerber_job requires non-empty member_kinds")
            expected = [kind for kind in MemberKind if kind in self.member_kinds]
            if self.member_kinds != expected:
                raise ValueError("member_kinds must be unique and ordered gerber, drill, gtd")
            if self.unsupported_reason_code is not None:
                raise ValueError("gerber_job cannot have unsupported_reason_code")
        else:
            if "member_kinds" in self.model_fields_set:
                raise ValueError("member_kinds must be omitted unless kind is gerber_job")
            if self.kind == MaterialKind.UNSUPPORTED:
                if self.unsupported_reason_code is None:
                    raise ValueError("unsupported material requires unsupported_reason_code")
            elif self.unsupported_reason_code is not None:
                raise ValueError("unsupported_reason_code is only valid for unsupported material")
        return self


class RunInputGroups(ContractModel):
    pdf_inputs: list[NonEmptyStr]
    image_inputs: list[NonEmptyStr]
    spreadsheet_inputs: list[NonEmptyStr]
    gerber_jobs: list[NonEmptyStr]
    unsupported_materials: list[NonEmptyStr]

    @model_validator(mode="after")
    def validate_groups(self) -> RunInputGroups:
        groups = (
            self.pdf_inputs,
            self.image_inputs,
            self.spreadsheet_inputs,
            self.gerber_jobs,
            self.unsupported_materials,
        )
        for group in groups:
            _ensure_unique(group, field_name="RunInputGroups")
        all_ids = [material_id for group in groups for material_id in group]
        _ensure_unique(all_ids, field_name="RunInputGroups across groups")
        return self


class SelectedRuleSet(ContractModel):
    mode: Literal["selected"]
    rule_set_id: NonEmptyStr
    rule_set_name: NonEmptyStr
    rule_set_version: NonEmptyStr
    schema_version: SchemaVersion
    source_kind: Literal["system_rule_set_file"]
    source_path: SystemRulePath
    content_sha256: Sha256
    snapshot_artifact: RunArtifactPath


class NoRuleSet(ContractModel):
    mode: Literal["not_selected"]
    reason: Literal["rule_set_not_selected"]


RuleSetSelection: TypeAlias = Annotated[
    SelectedRuleSet | NoRuleSet,
    Field(discriminator="mode"),
]


class RunInputSnapshot(ContractModel):
    schema_version: SchemaVersion
    project_id: NonEmptyStr
    run_id: NonEmptyStr
    snapshot_id: NonEmptyStr
    materials: list[RunInputMaterial]
    groups: RunInputGroups
    field_table: FieldTable
    rule_set: RuleSetSelection

    @model_validator(mode="after")
    def validate_snapshot(self) -> RunInputSnapshot:
        ids = [material.material_id for material in self.materials]
        _ensure_unique(ids, field_name="materials.material_id")
        orders = [material.material_order for material in self.materials]
        if orders != list(range(1, len(orders) + 1)):
            raise ValueError("materials must have contiguous material_order starting at 1")
        expected_by_kind = {
            MaterialKind.PDF: self.groups.pdf_inputs,
            MaterialKind.IMAGE: self.groups.image_inputs,
            MaterialKind.SPREADSHEET: self.groups.spreadsheet_inputs,
            MaterialKind.GERBER_JOB: self.groups.gerber_jobs,
            MaterialKind.UNSUPPORTED: self.groups.unsupported_materials,
        }
        for kind, actual in expected_by_kind.items():
            expected = [
                material.material_id for material in self.materials if material.kind == kind
            ]
            if actual != expected:
                raise ValueError(f"group for {kind.value} must exactly follow material_order")
        return self


class Region(FrozenContractModel):
    x: NonNegativeInt
    y: NonNegativeInt
    width: PositiveInt
    height: PositiveInt
    unit: Literal["px"]


class PdfPageEvidenceSeed(FrozenContractModel):
    evidence_id: EvidenceId
    type: Literal["pdf_page"]
    source_material_id: NonEmptyStr
    file_path: ProjectSourceRef
    page_index: PositiveInt
    region: Region | None
    note: StrictStr | None
    source_asset_path: RunArtifactPath

    @model_validator(mode="after")
    def validate_id(self) -> PdfPageEvidenceSeed:
        if not self.evidence_id.startswith("ev_pdf_"):
            raise ValueError("pdf_page evidence_id must use ev_pdf_ prefix")
        return self


class ImageEvidenceSeed(FrozenContractModel):
    evidence_id: EvidenceId
    type: Literal["image"]
    source_material_id: NonEmptyStr
    file_path: ProjectSourceRef
    page_index: None
    region: Region | None
    note: StrictStr | None
    source_asset_path: RunArtifactPath
    source_image_index: PositiveInt

    @model_validator(mode="after")
    def validate_id(self) -> ImageEvidenceSeed:
        if not self.evidence_id.startswith("ev_image_"):
            raise ValueError("image evidence_id must use ev_image_ prefix")
        return self


class GerberArtifactEvidenceSeed(FrozenContractModel):
    evidence_id: EvidenceId
    type: Literal["gerber_artifact"]
    source_material_id: NonEmptyStr
    file_path: ProjectSourceRef
    page_index: None
    region: None
    note: StrictStr | None
    artifact_kind: GerberArtifactKind
    artifact_path: RunArtifactPath

    @model_validator(mode="after")
    def validate_id(self) -> GerberArtifactEvidenceSeed:
        if not self.evidence_id.startswith("ev_gerber_"):
            raise ValueError("gerber_artifact evidence_id must use ev_gerber_ prefix")
        return self


EvidenceSeed: TypeAlias = Annotated[
    PdfPageEvidenceSeed | ImageEvidenceSeed | GerberArtifactEvidenceSeed,
    Field(discriminator="type"),
]


class ErrorRecord(FrozenContractModel):
    error_id: ErrorId
    code: NonEmptyStr
    severity: Severity
    phase: PhaseCode | None
    branch: BranchCode | None
    message: NonEmptyStr
    source_material_ids: list[NonEmptyStr]
    artifact_paths: list[RunArtifactPath]
    suggested_action: NonEmptyStr

    @model_validator(mode="after")
    def validate_error(self) -> ErrorRecord:
        _ensure_unique(self.source_material_ids, field_name="source_material_ids")
        _ensure_unique(self.artifact_paths, field_name="artifact_paths")
        if self.phase is None and self.branch is not None:
            raise ValueError("branch must be null when phase is null")
        if self.branch is not None:
            if self.phase == PhaseCode.GERBER_PREPARATION and self.branch != BranchCode.GERBER:
                raise ValueError("gerber_preparation only permits branch=gerber")
            if self.phase not in {PhaseCode.BRANCH_ANALYSIS, PhaseCode.GERBER_PREPARATION}:
                raise ValueError("branch is invalid for this error phase")
        return self


class ToolFinding(FrozenContractModel):
    finding_id: FindingId
    producer: NonEmptyStr
    code: NonEmptyStr
    kind: ToolFindingKind
    message: NonEmptyStr
    source_material_ids: list[NonEmptyStr]
    evidence_ids: list[EvidenceId]
    error_id: ErrorId | None

    @model_validator(mode="after")
    def validate_finding(self) -> ToolFinding:
        _ensure_unique(self.source_material_ids, field_name="source_material_ids")
        _ensure_unique(self.evidence_ids, field_name="evidence_ids")
        return self


class BranchField(FrozenContractModel):
    field_code: NonEmptyStr
    field_name: NonEmptyStr
    field_value: NonEmptyStr | None
    unit: NonEmptyStr | None
    category: FieldCategory
    branch_state: BranchFieldState
    source: NonEmptyStr
    source_material_id: NonEmptyStr
    source_ref: NonEmptyStr
    evidence_ids: list[EvidenceId]
    tool_finding_ids: list[FindingId]
    error_ids: list[ErrorId]
    check_note: StrictStr

    @model_validator(mode="after")
    def validate_field(self) -> BranchField:
        for name, values in (
            ("evidence_ids", self.evidence_ids),
            ("tool_finding_ids", self.tool_finding_ids),
            ("error_ids", self.error_ids),
        ):
            _ensure_unique(values, field_name=name)
        if self.branch_state == BranchFieldState.MISSING:
            if self.field_value is not None:
                raise ValueError("missing branch field must have null field_value")
        elif self.field_value is None:
            raise ValueError("non-missing branch field must have a real field_value")
        return self


class BranchResult(FrozenContractModel):
    schema_version: SchemaVersion
    run_id: NonEmptyStr
    branch: BranchCode
    status: BranchResultStatus
    source_material_ids: list[NonEmptyStr]
    fields: list[BranchField]
    unknown_notes: list[BranchField]
    tool_findings: list[ToolFinding]
    evidence: list[EvidenceSeed]
    errors: list[ErrorRecord]

    @model_validator(mode="after")
    def validate_result(self) -> BranchResult:
        if self.status == BranchResultStatus.SKIPPED:
            raise ValueError("skipped branches do not persist a BranchResult")
        if not self.source_material_ids:
            raise ValueError("persisted BranchResult requires source_material_ids")
        _ensure_unique(self.source_material_ids, field_name="source_material_ids")

        evidence_by_id = {item.evidence_id: item for item in self.evidence}
        finding_by_id = {item.finding_id: item for item in self.tool_findings}
        error_by_id = {item.error_id: item for item in self.errors}
        if len(evidence_by_id) != len(self.evidence):
            raise ValueError("evidence_id must be unique")
        if len(finding_by_id) != len(self.tool_findings):
            raise ValueError("finding_id must be unique")
        if len(error_by_id) != len(self.errors):
            raise ValueError("error_id must be unique")

        material_ids = set(self.source_material_ids)
        for seed in self.evidence:
            if seed.source_material_id not in material_ids:
                raise ValueError("EvidenceSeed source must belong to the branch")
        for finding in self.tool_findings:
            if not set(finding.source_material_ids).issubset(material_ids):
                raise ValueError("ToolFinding source must belong to the branch")
            if not set(finding.evidence_ids).issubset(evidence_by_id):
                raise ValueError("ToolFinding contains a dangling evidence reference")
            if finding.error_id is not None and finding.error_id not in error_by_id:
                raise ValueError("ToolFinding contains a dangling error reference")
        for error in self.errors:
            if not set(error.source_material_ids).issubset(material_ids):
                raise ValueError("ErrorRecord source must belong to the branch")

        for field in [*self.fields, *self.unknown_notes]:
            if field.source_material_id not in material_ids:
                raise ValueError("BranchField source must belong to the branch")
            for evidence_id in field.evidence_ids:
                seed = evidence_by_id.get(evidence_id)
                if seed is None or seed.source_material_id != field.source_material_id:
                    raise ValueError("BranchField evidence must resolve to the same source")
            if not set(field.tool_finding_ids).issubset(finding_by_id):
                raise ValueError("BranchField contains a dangling finding reference")
            if not set(field.error_ids).issubset(error_by_id):
                raise ValueError("BranchField contains a dangling error reference")

        for field in self.fields:
            if field.field_code in {
                "unknown_note_candidate",
                "unknown_note_###",
                "project_analysis_findings",
            }:
                raise ValueError("fields contains a branch-forbidden field_code")
        for note in self.unknown_notes:
            if (
                note.field_code != "unknown_note_candidate"
                or note.field_name != "未知说明候选"
                or note.category != FieldCategory.BOARD
                or note.branch_state != BranchFieldState.UNKNOWN
            ):
                raise ValueError("unknown_notes must use the canonical candidate shape")

        if self.status in {
            BranchResultStatus.COMPLETED,
            BranchResultStatus.COMPLETED_WITH_WARNINGS,
        } and not (self.fields or self.unknown_notes):
            raise ValueError("completed branch requires an adoptable field or unknown note")
        return self


class ConflictCandidate(FrozenContractModel):
    field_value: NonEmptyStr
    source_material_id: NonEmptyStr
    source_ref: NonEmptyStr
    evidence_ids: list[EvidenceId]

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, value: list[str]) -> list[str]:
        _ensure_unique(value, field_name="evidence_ids")
        return value


class ProjectDataRow(FrozenContractModel):
    index: PositiveInt
    field_code: NonEmptyStr
    field_name: NonEmptyStr
    field_value: NonEmptyStr | None
    source: NonEmptyStr
    evidence_ids: list[EvidenceId]
    tool_finding_ids: list[FindingId]
    error_ids: list[ErrorId]
    conflict_candidates: list[ConflictCandidate]
    category: FieldCategory
    field_state: FieldState
    check_note: StrictStr
    check_conclusion: NonEmptyStr

    @model_validator(mode="after")
    def validate_row(self) -> ProjectDataRow:
        for name, values in (
            ("evidence_ids", self.evidence_ids),
            ("tool_finding_ids", self.tool_finding_ids),
            ("error_ids", self.error_ids),
        ):
            _ensure_unique(values, field_name=name)
        if self.field_state == FieldState.MISSING and self.field_value is not None:
            raise ValueError("missing row must have null field_value")
        if self.conflict_candidates:
            if self.field_state != FieldState.CONFLICT:
                raise ValueError("conflict_candidates require field_state=conflict")
            if len(self.conflict_candidates) < 2:
                raise ValueError("source conflict requires at least two candidates")
            if self.source != self.conflict_candidates[0].source_ref:
                raise ValueError("row source must equal the first conflict candidate source_ref")
            expected_evidence: list[str] = []
            seen: set[str] = set()
            for candidate in self.conflict_candidates:
                for evidence_id in candidate.evidence_ids:
                    if evidence_id not in seen:
                        seen.add(evidence_id)
                        expected_evidence.append(evidence_id)
            if self.evidence_ids != expected_evidence:
                raise ValueError("row evidence_ids must be the stable candidate evidence union")
        if self.field_code.startswith("unknown_note_") and self.field_code != "unknown_note_###":
            if re.fullmatch(r"unknown_note_[0-9]{3,}", self.field_code) is None:
                raise ValueError(
                    "final unknown note code must use unknown_note_ plus a numeric suffix"
                )
        return self


class ProjectDataListArtifact(FrozenContractModel):
    schema_version: SchemaVersion
    run_id: NonEmptyStr
    rows: list[ProjectDataRow]

    @model_validator(mode="after")
    def validate_rows(self) -> ProjectDataListArtifact:
        indexes = [row.index for row in self.rows]
        if indexes != list(range(1, len(indexes) + 1)):
            raise ValueError("rows must have contiguous one-based indexes")
        _ensure_unique([row.field_code for row in self.rows], field_name="rows.field_code")
        return self


class Evidence(FrozenContractModel):
    evidence_id: EvidenceId
    type: EvidenceType
    source_material_id: NonEmptyStr
    file_path: ProjectSourceRef
    page_index: PositiveInt | None
    asset_path: RunArtifactPath
    region: Region | None
    note: StrictStr | None

    @model_validator(mode="after")
    def validate_evidence(self) -> Evidence:
        prefixes = {
            EvidenceType.PDF_PAGE: "ev_pdf_",
            EvidenceType.IMAGE: "ev_image_",
            EvidenceType.GERBER_ARTIFACT: "ev_gerber_",
        }
        if not self.evidence_id.startswith(prefixes[self.type]):
            raise ValueError("evidence_id prefix does not match evidence type")
        if self.type == EvidenceType.PDF_PAGE:
            if self.page_index is None:
                raise ValueError("pdf_page requires page_index")
        elif self.page_index is not None:
            raise ValueError("only pdf_page may have page_index")
        if self.type == EvidenceType.GERBER_ARTIFACT and self.region is not None:
            raise ValueError("gerber_artifact region must be null")
        return self


class EvidenceMetadataResponse(FrozenContractModel):
    evidence_id: EvidenceId
    type: EvidenceType
    source_material_id: NonEmptyStr
    file_path: ProjectSourceRef
    page_index: PositiveInt | None
    region: Region | None
    note: StrictStr | None

    @model_validator(mode="after")
    def validate_metadata(self) -> EvidenceMetadataResponse:
        prefixes = {
            EvidenceType.PDF_PAGE: "ev_pdf_",
            EvidenceType.IMAGE: "ev_image_",
            EvidenceType.GERBER_ARTIFACT: "ev_gerber_",
        }
        if not self.evidence_id.startswith(prefixes[self.type]):
            raise ValueError("evidence_id prefix does not match evidence type")
        if self.type == EvidenceType.PDF_PAGE:
            if self.page_index is None:
                raise ValueError("pdf_page requires page_index")
        elif self.page_index is not None:
            raise ValueError("only pdf_page may have page_index")
        if self.type == EvidenceType.GERBER_ARTIFACT and self.region is not None:
            raise ValueError("gerber_artifact region must be null")
        return self


class PdfPageEvidenceLocator(FrozenContractModel):
    kind: Literal["pdf_page"]
    source_asset_path: RunArtifactPath


class ImageEvidenceLocator(FrozenContractModel):
    kind: Literal["image"]
    source_asset_path: RunArtifactPath
    source_image_index: PositiveInt


class GerberArtifactEvidenceLocator(FrozenContractModel):
    kind: Literal["gerber_artifact"]
    artifact_kind: GerberArtifactKind


EvidenceLocator: TypeAlias = Annotated[
    PdfPageEvidenceLocator | ImageEvidenceLocator | GerberArtifactEvidenceLocator,
    Field(discriminator="kind"),
]


class EvidenceIndex(FrozenContractModel):
    schema_version: SchemaVersion
    run_id: NonEmptyStr
    evidence: dict[str, Evidence]
    locators: dict[str, EvidenceLocator]

    @model_validator(mode="after")
    def validate_index(self) -> EvidenceIndex:
        if set(self.evidence) != set(self.locators):
            raise ValueError("evidence and locators must have identical key sets")
        for key, item in self.evidence.items():
            _evidence_id(key)
            if key != item.evidence_id:
                raise ValueError("evidence map key must equal evidence_id")
            locator = self.locators[key]
            if locator.kind != item.type.value:
                raise ValueError("locator kind must match evidence type")
        return self


class ErrorSummary(FrozenContractModel):
    error_id: ErrorId
    code: NonEmptyStr
    severity: Severity
    phase: PhaseCode | None
    branch: BranchCode | None
    message: NonEmptyStr
    source_material_ids: list[NonEmptyStr]
    suggested_action: NonEmptyStr

    @model_validator(mode="after")
    def validate_error(self) -> ErrorSummary:
        _ensure_unique(self.source_material_ids, field_name="source_material_ids")
        if self.phase is None and self.branch is not None:
            raise ValueError("branch must be null when phase is null")
        if self.branch is not None:
            if self.phase == PhaseCode.GERBER_PREPARATION and self.branch != BranchCode.GERBER:
                raise ValueError("gerber_preparation only permits branch=gerber")
            if self.phase not in {PhaseCode.BRANCH_ANALYSIS, PhaseCode.GERBER_PREPARATION}:
                raise ValueError("branch is invalid for this error phase")
        return self


class IntegrationResult(ContractModel):
    draft_project_data_list: list[ProjectDataRow]
    project_data_list: list[ProjectDataRow] | None
    tool_findings: list[ToolFinding]
    errors: list[ErrorRecord]
    evidence_index: EvidenceIndex | None

    @model_validator(mode="after")
    def validate_result(self) -> IntegrationResult:
        _ensure_unique(
            [item.finding_id for item in self.tool_findings],
            field_name="tool_findings.finding_id",
        )
        _ensure_unique([item.error_id for item in self.errors], field_name="errors.error_id")
        return self


class ToolFindingsArtifact(FrozenContractModel):
    schema_version: SchemaVersion
    run_id: NonEmptyStr
    tool_findings: list[ToolFinding]

    @field_validator("tool_findings")
    @classmethod
    def validate_findings(cls, value: list[ToolFinding]) -> list[ToolFinding]:
        _ensure_unique([item.finding_id for item in value], field_name="tool_findings.finding_id")
        return value


class Rule(FrozenContractModel):
    rule_id: NonEmptyStr
    name: NonEmptyStr
    target_field_code: NonEmptyStr
    instruction: NonEmptyStr


class RuleSet(FrozenContractModel):
    schema_version: SchemaVersion
    rule_set_id: NonEmptyStr
    rule_set_name: NonEmptyStr
    rule_set_version: NonEmptyStr
    description: NonEmptyStr
    rules: list[Rule]

    @field_validator("rules")
    @classmethod
    def validate_rules(cls, value: list[Rule]) -> list[Rule]:
        _ensure_unique([rule.rule_id for rule in value], field_name="rules.rule_id")
        if any(rule.target_field_code == "unknown_note_###" for rule in value):
            raise ValueError("unknown_note_### cannot be a rule target")
        return value


class RuleSetSummary(FrozenContractModel):
    rule_set_id: NonEmptyStr
    rule_set_name: NonEmptyStr
    rule_set_version: NonEmptyStr
    schema_version: SchemaVersion
    description: NonEmptyStr
    source_kind: Literal["system_rule_set_file"]
    source_path: SystemRulePath
    content_sha256: Sha256
    rule_count: NonNegativeInt
    registered_at: AwareTimestamp
    updated_at: AwareTimestamp
    last_validated_at: AwareTimestamp

    @model_validator(mode="after")
    def validate_times(self) -> RuleSetSummary:
        if self.updated_at < self.registered_at:
            raise ValueError("updated_at cannot be earlier than registered_at")
        if self.last_validated_at < self.registered_at:
            raise ValueError("last_validated_at cannot be earlier than registered_at")
        return self


class ValidationMessage(FrozenContractModel):
    code: NonEmptyStr
    message: NonEmptyStr
    path: NonEmptyStr | None = Field(default=None, exclude_if=lambda value: value is None)


class RuleSetValidationResult(FrozenContractModel):
    validation_status: Literal["valid", "invalid"]
    rule_set_summary: RuleSetSummary | None
    errors: list[ValidationMessage]
    warnings: list[ValidationMessage]

    @model_validator(mode="after")
    def validate_result(self) -> RuleSetValidationResult:
        if self.validation_status == "valid":
            if self.rule_set_summary is None or self.errors:
                raise ValueError("valid result requires a summary and no errors")
        elif self.rule_set_summary is not None or not self.errors:
            raise ValueError("invalid result requires null summary and at least one error")
        return self


class RuleSetSnapshot(FrozenContractModel):
    rule_set_id: NonEmptyStr
    rule_set_name: NonEmptyStr
    rule_set_version: NonEmptyStr
    schema_version: SchemaVersion
    source_kind: Literal["system_rule_set_file"]
    source_path: SystemRulePath
    content_sha256: Sha256
    snapshot_created_at: AwareTimestamp
    rules: list[Rule]

    @field_validator("rules")
    @classmethod
    def validate_rules(cls, value: list[Rule]) -> list[Rule]:
        _ensure_unique([rule.rule_id for rule in value], field_name="rules.rule_id")
        if any(rule.target_field_code == "unknown_note_###" for rule in value):
            raise ValueError("unknown_note_### cannot be a rule target")
        return value


class RuleInstruction(FrozenContractModel):
    decision: RuleDecision
    append_check_note: NonEmptyStr
    check_conclusion: NonEmptyStr | None

    @model_validator(mode="after")
    def validate_instruction(self) -> RuleInstruction:
        if self.decision == RuleDecision.NO_VIOLATION:
            if self.check_conclusion is not None:
                raise ValueError("no_violation requires null check_conclusion")
        elif self.check_conclusion is None:
            raise ValueError("violation and insufficient_information require check_conclusion")
        return self


class RuleCheckIdentity(FrozenContractModel):
    rule_set_id: NonEmptyStr
    rule_set_version: NonEmptyStr
    content_sha256: Sha256


class RuleCheckResult(FrozenContractModel):
    schema_version: SchemaVersion
    run_id: NonEmptyStr
    status: RuleCheckResultStatus
    reason: NonEmptyStr | None = Field(default=None, exclude_if=lambda value: value is None)
    rule_set: RuleCheckIdentity | None
    executed_rule_ids: list[NonEmptyStr]
    attempted_rule_count: NonNegativeInt | None
    errors: list[ErrorRecord]

    @model_validator(mode="after")
    def validate_result(self) -> RuleCheckResult:
        _ensure_unique(self.executed_rule_ids, field_name="executed_rule_ids")
        _ensure_unique([error.error_id for error in self.errors], field_name="errors.error_id")
        if self.status == RuleCheckResultStatus.SKIPPED:
            if (
                self.reason != "rule_set_not_selected"
                or self.rule_set is not None
                or self.executed_rule_ids
                or self.attempted_rule_count != 0
                or self.errors
            ):
                raise ValueError("skipped RuleCheckResult has an invalid field combination")
        elif self.status == RuleCheckResultStatus.INTERRUPTED:
            if (
                self.reason is None
                or self.rule_set is None
                or self.attempted_rule_count is not None
            ):
                raise ValueError("interrupted result requires reason/rule_set and null count")
        else:
            if (
                self.reason is not None
                or self.rule_set is None
                or self.attempted_rule_count is None
            ):
                raise ValueError("completed result requires rule_set/count and no reason")
            if self.status == RuleCheckResultStatus.COMPLETED and self.errors:
                raise ValueError("completed result cannot contain errors")
            if self.status == RuleCheckResultStatus.COMPLETED_WITH_ERRORS and not self.errors:
                raise ValueError("completed_with_errors requires at least one error")
        return self


class RuleTraceArtifacts(FrozenContractModel):
    request: RunArtifactPath | None = Field(default=None, exclude_if=lambda value: value is None)
    response: RunArtifactPath | None = Field(default=None, exclude_if=lambda value: value is None)
    parsed: RunArtifactPath | None = Field(default=None, exclude_if=lambda value: value is None)


class RuleTraceEntry(FrozenContractModel):
    schema_version: SchemaVersion
    run_id: NonEmptyStr
    sequence: PositiveInt
    rule_id: NonEmptyStr
    target_field_code: NonEmptyStr
    model_called: StrictBool
    protocol_valid: StrictBool | None
    applied: StrictBool
    decision: RuleDecision | None
    skip_reason: Literal["target_row_not_present"] | None
    error_id: ErrorId | None
    artifacts: RuleTraceArtifacts

    @model_validator(mode="after")
    def validate_trace(self) -> RuleTraceEntry:
        if self.skip_reason == "target_row_not_present":
            if (
                self.model_called
                or self.protocol_valid is not None
                or self.applied
                or self.decision is not None
                or self.error_id is not None
                or any(
                    value is not None
                    for value in (
                        self.artifacts.request,
                        self.artifacts.response,
                        self.artifacts.parsed,
                    )
                )
            ):
                raise ValueError("target-row skip has an invalid trace combination")
            return self
        if self.decision is not None and self.protocol_valid is not True:
            raise ValueError("decision is only valid after successful protocol validation")
        if self.applied and (self.protocol_valid is not True or self.decision is None):
            raise ValueError("applied trace requires a valid decision")
        if self.protocol_valid is False and (self.applied or self.decision is not None):
            raise ValueError("invalid protocol cannot be applied")
        if self.model_called and self.artifacts.request is None:
            raise ValueError("a model call requires an actual request artifact")
        return self


class RuleCheckSummary(FrozenContractModel):
    status: RuleCheckStatus
    rule_set_id: NonEmptyStr | None
    rule_set_version: NonEmptyStr | None
    attempted_rule_count: NonNegativeInt | None
    error_count: NonNegativeInt | None
    reason: NonEmptyStr | None

    @model_validator(mode="after")
    def validate_summary(self) -> RuleCheckSummary:
        if self.status == RuleCheckStatus.SKIPPED:
            if (
                self.rule_set_id is not None
                or self.rule_set_version is not None
                or self.attempted_rule_count != 0
                or self.error_count != 0
                or self.reason != "rule_set_not_selected"
            ):
                raise ValueError("skipped rule summary has an invalid field combination")
        elif self.status == RuleCheckStatus.NOT_REACHED:
            if (
                self.rule_set_id is None
                or self.rule_set_version is None
                or self.attempted_rule_count != 0
                or self.error_count != 0
                or self.reason
                not in {
                    "awaiting_rule_check",
                    "run_stopped",
                    "upstream_failed",
                    "service_interrupted",
                }
            ):
                raise ValueError("not_reached rule summary has an invalid field combination")
        elif self.status == RuleCheckStatus.RUNNING:
            if (
                self.rule_set_id is None
                or self.rule_set_version is None
                or self.attempted_rule_count is not None
                or self.error_count is not None
                or self.reason is not None
            ):
                raise ValueError("running rule summary has an invalid field combination")
        elif self.status in {RuleCheckStatus.COMPLETED, RuleCheckStatus.COMPLETED_WITH_ERRORS}:
            if (
                self.rule_set_id is None
                or self.rule_set_version is None
                or self.attempted_rule_count is None
                or self.error_count is None
                or self.reason is not None
            ):
                raise ValueError("completed rule summary has an invalid field combination")
            if self.status == RuleCheckStatus.COMPLETED and self.error_count != 0:
                raise ValueError("completed rule summary requires error_count=0")
            if self.status == RuleCheckStatus.COMPLETED_WITH_ERRORS and self.error_count == 0:
                raise ValueError("completed_with_errors requires error_count > 0")
        elif (
            self.rule_set_id is None
            or self.rule_set_version is None
            or self.attempted_rule_count is not None
            or self.error_count is not None
            or self.reason
            not in {
                "run_stopped",
                "service_interrupted",
                "rule_trace_incomplete",
                "rule_check_failed",
            }
        ):
            raise ValueError("interrupted rule summary has an invalid field combination")
        return self


class PhaseTiming(FrozenContractModel):
    phase: PhaseCode
    scope_type: ScopeType
    scope_id: NonEmptyStr | None
    status: PhaseTimingStatus
    started_at: AwareTimestamp
    finished_at: AwareTimestamp | None
    duration_ms: NonNegativeInt | None

    @model_validator(mode="after")
    def validate_timing(self) -> PhaseTiming:
        if self.scope_type == ScopeType.PHASE:
            if self.scope_id is not None or self.phase in {
                PhaseCode.BRANCH_ANALYSIS,
                PhaseCode.GERBER_PREPARATION,
            }:
                raise ValueError("phase scope requires a serial phase and null scope_id")
        elif self.scope_type == ScopeType.BRANCH:
            if self.phase != PhaseCode.BRANCH_ANALYSIS or self.scope_id not in {
                branch.value for branch in BranchCode
            }:
                raise ValueError("branch scope requires branch_analysis and a branch code")
        elif self.phase != PhaseCode.GERBER_PREPARATION or self.scope_id is None:
            raise ValueError("gerber_job scope requires gerber_preparation and a job id")

        if self.status == PhaseTimingStatus.RUNNING:
            if self.finished_at is not None or self.duration_ms is not None:
                raise ValueError("running timing requires null finish and duration")
            return self
        if self.finished_at is None or self.duration_ms is None:
            raise ValueError("terminal timing requires finish and duration")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot be earlier than started_at")
        expected = _duration_ms(self.started_at, self.finished_at)
        if self.duration_ms != expected:
            raise ValueError("duration_ms does not match timing timestamps")
        if self.status == PhaseTimingStatus.SKIPPED and (
            self.finished_at != self.started_at or self.duration_ms != 0
        ):
            raise ValueError("skipped timing must be zero-duration")
        return self


class PhaseTimingResponse(FrozenContractModel):
    phase: PhaseCode
    scope_type: ScopeType
    scope_id: NonEmptyStr | None
    status: PhaseTimingStatus
    started_at: AwareTimestamp
    finished_at: AwareTimestamp | None
    duration_ms: NonNegativeInt

    @model_validator(mode="after")
    def validate_timing(self) -> PhaseTimingResponse:
        if self.status == PhaseTimingStatus.RUNNING:
            if self.finished_at is not None:
                raise ValueError("running response timing keeps finished_at null")
        elif self.finished_at is None:
            raise ValueError("terminal response timing requires finished_at")
        return self


class BranchStatusMap(FrozenContractModel):
    pdf_image: BranchStatus
    xlsx: BranchStatus
    gerber: BranchStatus


class ModelCallSummary(FrozenContractModel):
    call_purpose: CallPurpose
    logical_call_count: PositiveInt
    success_count: NonNegativeInt
    failure_count: NonNegativeInt
    total_duration_ms: NonNegativeInt
    transport_retry_count: NonNegativeInt

    @model_validator(mode="after")
    def validate_counts(self) -> ModelCallSummary:
        if self.logical_call_count != self.success_count + self.failure_count:
            raise ValueError("logical_call_count must equal success_count plus failure_count")
        if self.transport_retry_count > self.logical_call_count:
            raise ValueError("transport_retry_count cannot exceed logical_call_count")
        return self


class RunSummary(FrozenContractModel):
    schema_version: SchemaVersion
    run_id: NonEmptyStr
    project_id: NonEmptyStr
    status: RunStatus
    current_phase: PhaseCode | None
    phase_timings: list[PhaseTiming]
    branch_status: BranchStatusMap
    rule_check_summary: RuleCheckSummary
    model_call_summaries: list[ModelCallSummary]
    error_summary: list[ErrorRecord]
    stop_requested_at: AwareTimestamp | None
    stop_reason: Literal["user_requested"] | None
    started_at: AwareTimestamp
    finished_at: AwareTimestamp | None
    duration_ms: NonNegativeInt | None
    artifact_index_path: RunArtifactPath | None
    project_data_list_path: RunArtifactPath | None

    @model_validator(mode="after")
    def validate_summary(self) -> RunSummary:
        identities = [(item.phase, item.scope_type, item.scope_id) for item in self.phase_timings]
        if len(identities) != len(set(identities)):
            raise ValueError("phase timing identity must be unique")
        _ensure_unique([item.error_id for item in self.error_summary], field_name="error_summary")
        model_call_purposes = [item.call_purpose for item in self.model_call_summaries]
        _ensure_unique(model_call_purposes, field_name="model_call_summaries.call_purpose")
        expected_model_call_order = [
            purpose for purpose in CallPurpose if purpose in set(model_call_purposes)
        ]
        if model_call_purposes != expected_model_call_order:
            raise ValueError("model_call_summaries must use stable call purpose order")
        for timing in self.phase_timings:
            if timing.started_at < self.started_at:
                raise ValueError("phase timing cannot start before the run")

        if self.status in {RunStatus.QUEUED, RunStatus.RUNNING}:
            if self.finished_at is not None or self.duration_ms is not None:
                raise ValueError("non-terminal run must persist null finish and duration")
            if self.status == RunStatus.QUEUED and self.current_phase is not None:
                raise ValueError("queued run must have null current_phase")
        else:
            if self.finished_at is None or self.duration_ms is None:
                raise ValueError("terminal run requires finish and duration")
            if self.finished_at < self.started_at:
                raise ValueError("finished_at cannot be earlier than started_at")
            if self.duration_ms != _duration_ms(self.started_at, self.finished_at):
                raise ValueError("duration_ms does not match run timestamps")
            if any(
                timing.finished_at is not None and timing.finished_at > self.finished_at
                for timing in self.phase_timings
            ):
                raise ValueError("phase timing cannot finish after the run")

        if self.status == RunStatus.STOPPED:
            if self.stop_requested_at is None or self.stop_reason != "user_requested":
                raise ValueError("stopped run requires the user stop fields")
        elif self.stop_reason is not None:
            raise ValueError("stop_reason is only valid for stopped runs")
        if self.stop_requested_at is not None and self.stop_requested_at < self.started_at:
            raise ValueError("stop_requested_at cannot be earlier than started_at")
        if self.status in {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_ERRORS}:
            if self.project_data_list_path is None:
                raise ValueError("completed run requires project_data_list_path")
        has_error = any(error.severity == Severity.ERROR for error in self.error_summary)
        if self.status == RunStatus.COMPLETED and has_error:
            raise ValueError("completed run cannot contain severity=error")
        if self.status == RunStatus.COMPLETED_WITH_ERRORS and not has_error:
            raise ValueError("completed_with_errors requires severity=error")
        return self


class RunSummaryResponse(FrozenContractModel):
    schema_version: SchemaVersion
    run_id: NonEmptyStr
    project_id: NonEmptyStr
    status: RunStatus
    current_phase: PhaseCode | None
    phase_timings: list[PhaseTimingResponse]
    branch_status: BranchStatusMap
    rule_check_summary: RuleCheckSummary
    model_call_summaries: list[ModelCallSummary]
    error_summary: list[ErrorSummary]
    stop_requested_at: AwareTimestamp | None
    stop_reason: Literal["user_requested"] | None
    started_at: AwareTimestamp
    finished_at: AwareTimestamp | None
    duration_ms: NonNegativeInt | None

    @model_validator(mode="after")
    def validate_summary(self) -> RunSummaryResponse:
        model_call_purposes = [item.call_purpose for item in self.model_call_summaries]
        _ensure_unique(model_call_purposes, field_name="model_call_summaries.call_purpose")
        expected_model_call_order = [
            purpose for purpose in CallPurpose if purpose in set(model_call_purposes)
        ]
        if model_call_purposes != expected_model_call_order:
            raise ValueError("model_call_summaries must use stable call purpose order")
        if self.status == RunStatus.RUNNING and self.duration_ms is None:
            raise ValueError("running response requires materialized duration_ms")
        if self.status == RunStatus.QUEUED and self.duration_ms is not None:
            raise ValueError("queued response keeps duration_ms null")
        if self.status not in {RunStatus.QUEUED, RunStatus.RUNNING} and self.duration_ms is None:
            raise ValueError("terminal response requires duration_ms")
        return self


class DataListUnavailableSummary(FrozenContractModel):
    kind: Literal["data_list_unavailable"]
    run_status: Literal["queued", "running", "stopped", "failed"]
    reason_code: NonEmptyStr | None
    current_phase: PhaseCode | None
    branch_status: BranchStatusMap
    rule_check_summary: RuleCheckSummary
    error_summary: list[ErrorSummary]
    stop_requested_at: AwareTimestamp | None
    stop_reason: Literal["user_requested"] | None
    run_summary_endpoint: NonEmptyStr

    @model_validator(mode="after")
    def validate_summary(self) -> DataListUnavailableSummary:
        if self.run_status == "stopped":
            if (
                self.reason_code != "user_requested"
                or self.stop_requested_at is None
                or self.stop_reason != "user_requested"
            ):
                raise ValueError("stopped data-list summary requires user stop fields")
        elif self.stop_reason is not None:
            raise ValueError("stop_reason is only valid for stopped summary")
        if not self.run_summary_endpoint.startswith("/"):
            raise ValueError("run_summary_endpoint must be an API path")
        return self


class TrajectoryEvent(FrozenContractModel):
    schema_version: SchemaVersion
    run_id: NonEmptyStr
    timestamp: AwareTimestamp
    phase: PhaseCode | None
    branch: BranchCode | None
    event_type: TrajectoryEventType
    summary: dict[str, FiniteJsonScalar]
    duration_ms: NonNegativeInt | None = Field(default=None, exclude_if=lambda value: value is None)
    source_material_ids: list[NonEmptyStr]
    artifact_paths: list[RunArtifactPath]
    error_ids: list[ErrorId]

    @model_validator(mode="after")
    def validate_event(self) -> TrajectoryEvent:
        for name, values in (
            ("source_material_ids", self.source_material_ids),
            ("artifact_paths", self.artifact_paths),
            ("error_ids", self.error_ids),
        ):
            _ensure_unique(values, field_name=name)
        if self.phase is None and self.branch is not None:
            raise ValueError("branch must be null when phase is null")
        if self.branch is not None and self.phase not in {
            PhaseCode.BRANCH_ANALYSIS,
            PhaseCode.GERBER_PREPARATION,
        }:
            raise ValueError("branch is invalid for this phase")
        if self.event_type == TrajectoryEventType.PHASE_STARTED and self.duration_ms is not None:
            raise ValueError("phase_started must omit duration_ms")
        return self


class ArtifactIndexEntry(FrozenContractModel):
    path: RunArtifactPath
    kind: NonEmptyStr
    producer_phase: PhaseCode
    consumers: list[ArtifactConsumer]
    access_level: AccessLevel
    source_material_ids: list[NonEmptyStr]

    @model_validator(mode="after")
    def validate_entry(self) -> ArtifactIndexEntry:
        if not self.consumers:
            raise ValueError("consumers must not be empty")
        _ensure_unique(self.consumers, field_name="consumers")
        _ensure_unique(self.source_material_ids, field_name="source_material_ids")
        return self


class ArtifactIndex(FrozenContractModel):
    schema_version: SchemaVersion
    run_id: NonEmptyStr
    entries: list[ArtifactIndexEntry]

    @field_validator("entries")
    @classmethod
    def validate_entries(cls, value: list[ArtifactIndexEntry]) -> list[ArtifactIndexEntry]:
        _ensure_unique([entry.path for entry in value], field_name="entries.path")
        return value


class LayerInput(FrozenContractModel):
    layer_id: NonEmptyStr
    file_path: GerberMemberPath
    detected_type: DetectedLayerType


GerberLayerInput = LayerInput


class GerberJobPreparation(FrozenContractModel):
    schema_version: SchemaVersion
    project_id: NonEmptyStr
    run_id: NonEmptyStr
    job_material_id: NonEmptyStr
    layer_inputs: list[LayerInput]

    @model_validator(mode="after")
    def validate_inputs(self) -> GerberJobPreparation:
        _ensure_unique(
            [item.layer_id for item in self.layer_inputs], field_name="layer_inputs.layer_id"
        )
        _ensure_unique(
            [item.file_path for item in self.layer_inputs],
            field_name="layer_inputs.file_path",
        )
        return self


class LayerDefinitionItem(FrozenContractModel):
    layer_id: NonEmptyStr
    file_path: GerberMemberPath
    role: LayerRole
    copper_layer_index: PositiveInt | None

    @model_validator(mode="after")
    def validate_item(self) -> LayerDefinitionItem:
        copper_roles = {
            LayerRole.TOP_COPPER,
            LayerRole.INNER_COPPER,
            LayerRole.BOTTOM_COPPER,
        }
        if self.role in copper_roles:
            if self.copper_layer_index is None:
                raise ValueError("copper role requires copper_layer_index")
        elif self.copper_layer_index is not None:
            raise ValueError("non-copper role requires null copper_layer_index")
        if self.role == LayerRole.TOP_COPPER and self.copper_layer_index != 1:
            raise ValueError("top_copper requires copper_layer_index=1")
        return self


def _validate_layer_items(items: list[LayerDefinitionItem]) -> None:
    _ensure_unique([item.layer_id for item in items], field_name="layers.layer_id")
    _ensure_unique([item.file_path for item in items], field_name="layers.file_path")
    copper = [item for item in items if item.copper_layer_index is not None]
    _ensure_unique(
        [str(item.copper_layer_index) for item in copper],
        field_name="layers.copper_layer_index",
    )
    for role in (LayerRole.TOP_COPPER, LayerRole.BOTTOM_COPPER):
        if sum(item.role == role for item in items) > 1:
            raise ValueError(f"{role.value} may appear at most once")
    bottoms = [item.copper_layer_index for item in items if item.role == LayerRole.BOTTOM_COPPER]
    inners = [item.copper_layer_index for item in items if item.role == LayerRole.INNER_COPPER]
    if bottoms and inners and bottoms[0] <= max(inners):
        raise ValueError("bottom copper index must be greater than all inner copper indexes")


class LayerDefinition(FrozenContractModel):
    project_id: NonEmptyStr
    run_id: NonEmptyStr
    job_material_id: NonEmptyStr
    layers: list[LayerDefinitionItem]
    updated_at: AwareTimestamp
    confirmed_at: AwareTimestamp | None

    @model_validator(mode="after")
    def validate_definition(self) -> LayerDefinition:
        _validate_layer_items(self.layers)
        if self.confirmed_at is not None and self.confirmed_at < self.updated_at:
            raise ValueError("confirmed_at cannot be earlier than updated_at")
        return self


class LayerDefinitionRequest(FrozenContractModel):
    project_id: NonEmptyStr
    run_id: NonEmptyStr
    job_material_id: NonEmptyStr
    layers: list[LayerDefinitionItem]

    @field_validator("layers")
    @classmethod
    def validate_layers(cls, value: list[LayerDefinitionItem]) -> list[LayerDefinitionItem]:
        _validate_layer_items(value)
        return value


class LayerDefinitionsArtifact(FrozenContractModel):
    schema_version: SchemaVersion
    run_id: NonEmptyStr
    definitions: list[LayerDefinition]

    @model_validator(mode="after")
    def validate_definitions(self) -> LayerDefinitionsArtifact:
        _ensure_unique(
            [item.job_material_id for item in self.definitions],
            field_name="definitions.job_material_id",
        )
        if any(item.run_id != self.run_id for item in self.definitions):
            raise ValueError("definition run_id must match artifact run_id")
        if len({item.project_id for item in self.definitions}) > 1:
            raise ValueError("all definitions must belong to the same project")
        return self


class ApiProblem(FrozenContractModel):
    kind: Literal["api_problem"]
    code: NonEmptyStr
    message: NonEmptyStr
    status: HttpStatus
    error_id: ErrorId | None
    details: dict[str, JsonValue]


def to_project_summary(record: ProjectRecord) -> ProjectSummary:
    return ProjectSummary(
        project_id=record.project_id,
        project_name=record.project_name,
        material_count=len(record.materials),
        latest_run_id=record.latest_run_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def to_error_summary(error: ErrorRecord) -> ErrorSummary:
    return ErrorSummary(
        error_id=error.error_id,
        code=error.code,
        severity=error.severity,
        phase=error.phase,
        branch=error.branch,
        message=error.message,
        source_material_ids=list(error.source_material_ids),
        suggested_action=error.suggested_action,
    )


def to_evidence_metadata(evidence: Evidence) -> EvidenceMetadataResponse:
    return EvidenceMetadataResponse(
        evidence_id=evidence.evidence_id,
        type=evidence.type,
        source_material_id=evidence.source_material_id,
        file_path=evidence.file_path,
        page_index=evidence.page_index,
        region=evidence.region.model_copy(deep=True) if evidence.region is not None else None,
        note=evidence.note,
    )


def materialize_run_summary(
    summary: RunSummary,
    *,
    now: datetime,
) -> RunSummaryResponse:
    materialized_at = _parse_timestamp(now)
    timings: list[PhaseTimingResponse] = []
    for timing in summary.phase_timings:
        duration_ms = timing.duration_ms
        if timing.status == PhaseTimingStatus.RUNNING:
            duration_ms = _duration_ms(timing.started_at, materialized_at)
        if duration_ms is None:
            raise ValueError("terminal timing unexpectedly lacks duration_ms")
        timings.append(
            PhaseTimingResponse(
                phase=timing.phase,
                scope_type=timing.scope_type,
                scope_id=timing.scope_id,
                status=timing.status,
                started_at=timing.started_at,
                finished_at=timing.finished_at,
                duration_ms=duration_ms,
            )
        )

    duration_ms = summary.duration_ms
    if summary.status == RunStatus.RUNNING:
        duration_ms = _duration_ms(summary.started_at, materialized_at)
    return RunSummaryResponse(
        schema_version=summary.schema_version,
        run_id=summary.run_id,
        project_id=summary.project_id,
        status=summary.status,
        current_phase=summary.current_phase,
        phase_timings=timings,
        branch_status=summary.branch_status.model_copy(deep=True),
        rule_check_summary=summary.rule_check_summary.model_copy(deep=True),
        model_call_summaries=[item.model_copy(deep=True) for item in summary.model_call_summaries],
        error_summary=[to_error_summary(error) for error in summary.error_summary],
        stop_requested_at=summary.stop_requested_at,
        stop_reason=summary.stop_reason,
        started_at=summary.started_at,
        finished_at=summary.finished_at,
        duration_ms=duration_ms,
    )


def unwrap_project_data_list(artifact: ProjectDataListArtifact) -> list[ProjectDataRow]:
    return [row.model_copy(deep=True) for row in artifact.rows]
