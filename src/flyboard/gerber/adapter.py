from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from flyboard.models import (
    BranchField,
    BranchFieldState,
    ErrorRecord,
    GerberArtifactEvidenceSeed,
    RunInputSnapshot,
    ToolFinding,
)

from .diagnostics import GerberDiagnosticNoteWriter
from .models import (
    FIELD_CODES,
    OUTPUT_KEYS,
    GerberAdapterSummaryArtifact,
    GerberExpectedOutputMissing,
    GerberFieldOutcome,
    GerberResult,
)
from .parser import ParsedJob


@dataclass(frozen=True, slots=True)
class JobAdaptation:
    summary: GerberAdapterSummaryArtifact
    fields: list[BranchField]
    findings: list[ToolFinding]
    evidence: list[GerberArtifactEvidenceSeed]
    errors: list[ErrorRecord]


class GerberResultAdapter:
    def __init__(self, *, note_writer: GerberDiagnosticNoteWriter | None = None) -> None:
        self._notes = note_writer or GerberDiagnosticNoteWriter()

    def adapt(
        self,
        result: GerberResult,
        job: ParsedJob,
        snapshot: RunInputSnapshot,
        context,
    ) -> JobAdaptation:
        if result.run_id != context.run_id or result.job_material_id != job.job_material_id:
            raise ValueError("Gerber result identity mismatch")
        output_keys = [item.key for item in result.outputs]
        missing_keys = [key for key in OUTPUT_KEYS if key not in output_keys]
        if missing_keys:
            raise GerberExpectedOutputMissing(",".join(missing_keys))
        if set(output_keys) != set(OUTPUT_KEYS) or len(output_keys) != len(OUTPUT_KEYS):
            raise ValueError("Gerber result output key set is invalid")
        outputs_by_key = {item.key: item for item in result.outputs}
        ordered_outputs = [outputs_by_key[key] for key in OUTPUT_KEYS]
        field_by_code = {item.field_code: item for item in snapshot.field_table.fields}
        for output in ordered_outputs:
            if not output.applicable:
                continue
            field_code = FIELD_CODES[output.key]
            definition = field_by_code.get(field_code)
            if definition is None or definition.producer_scope.value != "branch_or_integration":
                raise ValueError("Gerber output maps outside the current FieldTable")
            if definition.unit != output.unit:
                raise ValueError("Gerber output unit differs from the current FieldTable")

        result_path = f"gerberscout/{job.job_material_id}/result.json"
        summary_path = f"gerberscout/{job.job_material_id}/adapter_summary.json"
        evidence = [
            _seed(job, context, "result", result_path, "GerberScout 结构化结果"),
            _seed(
                job,
                context,
                "adapter_summary",
                summary_path,
                "GerberScout 字段适配摘要",
            ),
        ]
        evidence_ids = [item.evidence_id for item in evidence]

        findings: list[ToolFinding] = []
        finding_ids_by_key: dict[str, list[str]] = {key: [] for key in OUTPUT_KEYS}
        for item in result.checks:
            finding = context.diagnostics.record_finding(
                producer="GerberScout",
                code=item.code,
                kind=item.kind,
                message=item.message,
                source_material_ids=[job.job_material_id],
                evidence_ids=evidence_ids,
                error_id=None,
            )
            findings.append(finding)
            for key in item.affected_output_keys:
                finding_ids_by_key[key].append(finding.finding_id)

        errors: list[ErrorRecord] = []
        error_ids_by_key: dict[str, list[str]] = {key: [] for key in OUTPUT_KEYS}
        for item in result.errors:
            error = context.diagnostics.record_error(
                code=item.code,
                severity=item.severity,
                phase="branch_analysis",
                branch="gerber",
                message=item.message,
                source_material_ids=[job.job_material_id],
                artifact_paths=list(item.artifact_paths),
                suggested_action=item.suggested_action,
            )
            errors.append(error)
            for key in item.affected_output_keys:
                error_ids_by_key[key].append(error.error_id)

        fields: list[BranchField] = []
        outcomes: list[GerberFieldOutcome] = []
        for output in ordered_outputs:
            if not output.applicable:
                continue
            field_code = FIELD_CODES[output.key]
            definition = field_by_code.get(field_code)
            if definition is None or definition.producer_scope.value != "branch_or_integration":
                raise ValueError("Gerber output maps outside the current FieldTable")
            finding_ids = list(finding_ids_by_key[output.key])
            error_ids = list(error_ids_by_key[output.key])
            status = (
                "available"
                if output.coverage == "complete"
                else "unreliable"
                if output.coverage == "partial"
                else "missing"
            )
            value = None if output.value is None else _format(output.key, output.value)
            outcome = GerberFieldOutcome(
                output_key=output.key,
                field_code=field_code,
                status=status,
                value=value,
                evidence_ids=evidence_ids,
                tool_finding_ids=finding_ids,
                error_ids=error_ids,
            )
            check_note = "GerberScout 已基于确认层定义和工具结果形成该字段候选。"
            if finding_ids or error_ids:
                mode = (
                    "missing_value"
                    if status == "missing"
                    else "unreliable_value"
                    if status == "unreliable"
                    else "finding_only"
                )
                note_result = self._notes.write(
                    job_material_id=job.job_material_id,
                    output_key=output.key,
                    mode=mode,
                    field_context={
                        "field": {
                            "field_name": definition.field_name,
                            "definition": definition.definition,
                            "field_value": value,
                            "unit": definition.unit,
                        },
                        "tool_findings": [
                            {"message": finding.message}
                            for finding in findings
                            if finding.finding_id in finding_ids
                        ],
                        "errors": [
                            {
                                "message": error.message,
                                "suggested_action": error.suggested_action,
                            }
                            for error in errors
                            if error.error_id in error_ids
                        ],
                    },
                    context=context,
                )
                check_note = note_result.note
                if note_result.error is not None:
                    errors = _append_error(errors, note_result.error)
                    error_ids = [*error_ids, note_result.error.error_id]
                    outcome = GerberFieldOutcome(
                        output_key=outcome.output_key,
                        field_code=outcome.field_code,
                        status=outcome.status,
                        value=outcome.value,
                        evidence_ids=outcome.evidence_ids,
                        tool_finding_ids=outcome.tool_finding_ids,
                        error_ids=error_ids,
                    )
            outcomes.append(outcome)
            fields.append(
                BranchField(
                    field_code=field_code,
                    field_name=definition.field_name,
                    field_value=value,
                    unit=definition.unit,
                    category=definition.category,
                    branch_state=(
                        BranchFieldState.CLEAR_NOW
                        if status == "available"
                        else BranchFieldState.UNKNOWN
                        if status == "unreliable"
                        else BranchFieldState.MISSING
                    ),
                    source="GerberScout",
                    source_material_id=job.job_material_id,
                    source_ref=job.source_ref,
                    evidence_ids=evidence_ids,
                    tool_finding_ids=finding_ids,
                    error_ids=error_ids,
                    check_note=check_note,
                )
            )

        summary = GerberAdapterSummaryArtifact(
            schema_version="1.0",
            run_id=context.run_id,
            job_material_id=job.job_material_id,
            field_outcomes=outcomes,
        )
        return JobAdaptation(
            summary=summary,
            fields=fields,
            findings=findings,
            evidence=evidence,
            errors=errors,
        )


def _seed(job, context, artifact_kind, artifact_path, note):
    evidence_id = context.evidence_id_helper.assign(
        evidence_type="gerber_artifact",
        source_material_id=job.job_material_id,
        file_path=job.source_ref,
        page_index=None,
        region=None,
        artifact_kind=artifact_kind,
        artifact_path=artifact_path,
    )
    return GerberArtifactEvidenceSeed(
        evidence_id=evidence_id,
        type="gerber_artifact",
        source_material_id=job.job_material_id,
        file_path=job.source_ref,
        page_index=None,
        region=None,
        note=note,
        artifact_kind=artifact_kind,
        artifact_path=artifact_path,
    )


def _format(key: str, value: object) -> str:
    if key == "layers.copper_layer_count":
        return str(value)
    if key in {"layers.solder_mask_sides", "layers.silkscreen_sides"}:
        return " + ".join(value)
    if key == "geometry.job_bounds_mm":
        return (
            f"min_x={_number(value['min_x'])}, min_y={_number(value['min_y'])}, "
            f"max_x={_number(value['max_x'])}, max_y={_number(value['max_y'])} mm"
        )
    if key in {
        "geometry.min_trace_width_mm",
        "geometry.min_copper_spacing_mm",
        "drill.min_used_hole_diameter_mm",
        "geometry.min_annular_ring_mm",
    }:
        return _number(value)
    if key == "drill.used_hole_size_summary":
        return ", ".join(f"{_number(item)} mm" for item in value)
    if key == "drill.used_hole_quantity_summary":
        return ", ".join(
            f"{_number(item['diameter_mm'])} mm × {item['quantity']}" for item in value
        )
    if key == "drill.slot_size_summary":
        return ", ".join(
            f"{_number(item['width_mm'])} × {_number(item['length_mm'])} mm × {item['quantity']}"
            for item in value
        )
    return str(value)


def _number(value) -> str:
    rendered = format(Decimal(str(value)), "f")
    rendered = rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    return rendered or "0"


def _append_error(values, error):
    if any(item.error_id == error.error_id for item in values):
        return values
    return [*values, error]
