from __future__ import annotations

from flyboard.errors import ServiceError
from flyboard.models import (
    AccessLevel,
    ArtifactConsumer,
    ArtifactIndexEntry,
    BranchCode,
    BranchFieldState,
    BranchResult,
    BranchResultStatus,
    ErrorRecord,
    GerberJobPreparation,
    LayerDefinition,
    LayerInput,
    PhaseCode,
    RunInputSnapshot,
)
from flyboard.repositories import ProjectRepository

from .adapter import GerberResultAdapter
from .engine import build_gerber_result
from .models import GerberExpectedOutputMissing
from .parser import (
    GerberJobParser,
    GerberPreparationError,
    ParsedJob,
    RunGerberCache,
    layer_id,
    render_layer_svg,
)


class GerberScoutAnalyzer:
    """Production Gerber preparation, 13-output adapter, and multi-job branch owner."""

    def __init__(
        self,
        projects: ProjectRepository,
        *,
        parser: GerberJobParser | None = None,
        cache: RunGerberCache | None = None,
        adapter: GerberResultAdapter | None = None,
    ) -> None:
        self._parser = parser or GerberJobParser(projects)
        self._cache = cache or RunGerberCache()
        self._adapter = adapter or GerberResultAdapter()

    def release_run(self, run_id: str) -> None:
        self._cache.discard_run(run_id)

    def prepare(
        self,
        snapshot: RunInputSnapshot,
        job_material_id: str,
        context,
    ) -> tuple[GerberJobPreparation | None, list[ErrorRecord]]:
        try:
            job = self._parser.parse(snapshot, job_material_id)
            self._cache.put(job)
            layers = [
                LayerInput(
                    layer_id=layer_id(item.relative_path),
                    file_path=item.relative_path,
                    detected_type=item.detected_type,
                )
                for item in job.layer_members
            ]
            for member, layer in zip(job.layer_members, layers):
                content = render_layer_svg(job, member)
                context.artifact_writer.write_content(
                    content,
                    "image/svg+xml",
                    ArtifactIndexEntry(
                        path=f"gerberscout/{job_material_id}/preview/{layer.layer_id}.svg",
                        kind="gerber_preview",
                        producer_phase=PhaseCode.GERBER_PREPARATION,
                        consumers=[
                            ArtifactConsumer.API,
                            ArtifactConsumer.GUI,
                            ArtifactConsumer.REGRESSION,
                        ],
                        access_level=AccessLevel.PROJECT_SCOPED,
                        source_material_ids=[job_material_id],
                    ),
                )
            preparation = GerberJobPreparation(
                schema_version="1.0",
                project_id=snapshot.project_id,
                run_id=snapshot.run_id,
                job_material_id=job_material_id,
                layer_inputs=layers,
            )
            context.artifact_writer.write_json(
                preparation,
                ArtifactIndexEntry(
                    path=f"gerberscout/{job_material_id}/preparation.json",
                    kind="gerber_preparation",
                    producer_phase=PhaseCode.GERBER_PREPARATION,
                    consumers=[
                        ArtifactConsumer.WORKFLOW,
                        ArtifactConsumer.API,
                        ArtifactConsumer.GUI,
                        ArtifactConsumer.REGRESSION,
                    ],
                    access_level=AccessLevel.PROJECT_SCOPED,
                    source_material_ids=[job_material_id],
                ),
            )
            return preparation, []
        except GerberPreparationError as exc:
            error = context.diagnostics.record_error(
                code=exc.code,
                severity="error",
                phase="gerber_preparation",
                branch="gerber",
                message=exc.message,
                source_material_ids=[job_material_id],
                artifact_paths=[],
                suggested_action="检查 Gerber/Drill/GTD job 内容后创建新的运行。",
            )
            return None, [error]
        except ServiceError as exc:
            if exc.error_id is None:
                raise
            return None, [context.diagnostics.resolve_error(exc.error_id)]
        except Exception:
            error = context.diagnostics.record_error(
                code="gerber_input_parse_failed",
                severity="error",
                phase="gerber_preparation",
                branch="gerber",
                message="Gerber preparation 未能形成完整 preview 与 canonical preparation。",
                source_material_ids=[job_material_id],
                artifact_paths=[],
                suggested_action="检查 GerberScout preparation 实现和输入资料。",
            )
            return None, [error]

    def analyze(
        self,
        snapshot: RunInputSnapshot,
        context,
        preparations: list[GerberJobPreparation],
        layer_definitions: list[LayerDefinition],
    ) -> BranchResult:
        source_ids = list(snapshot.groups.gerber_jobs)
        _validate_inputs(snapshot, preparations, layer_definitions)
        definitions_by_job = {item.job_material_id: item for item in layer_definitions}
        fields = []
        findings = []
        evidence = []
        errors: list[ErrorRecord] = []
        index_failed = False

        for preparation in preparations:
            context.stop_signal.check_boundary()
            job_id = preparation.job_material_id
            snapshot_sha256 = next(
                item.sha256 for item in snapshot.materials if item.material_id == job_id
            )
            job = self._cache.get(snapshot.run_id, job_id, snapshot_sha256)
            if job is None:
                try:
                    job = self._parser.parse(snapshot, job_id)
                    self._cache.put(job)
                except GerberPreparationError as exc:
                    errors = _append_error(
                        errors,
                        context.diagnostics.record_error(
                            code=exc.code,
                            severity="error",
                            phase="branch_analysis",
                            branch="gerber",
                            message=exc.message,
                            source_material_ids=[job_id],
                            artifact_paths=[],
                            suggested_action="检查当前 job 的受管输入和 parser 后创建新的运行。",
                        ),
                    )
                    continue
            try:
                _validate_preparation(job, preparation)
                result = build_gerber_result(job, definitions_by_job[job_id])
                adaptation = self._adapter.adapt(result, job, snapshot, context)
            except GerberExpectedOutputMissing:
                errors = _append_error(
                    errors,
                    context.diagnostics.record_error(
                        code="gerber_expected_output_missing",
                        severity="error",
                        phase="branch_analysis",
                        branch="gerber",
                        message="GerberResult 缺少一个或多个预期 output key。",
                        source_material_ids=[job_id],
                        artifact_paths=[],
                        suggested_action="检查 GerberScout 是否完整发射固定 13 项 output。",
                    ),
                )
                continue
            except ServiceError as exc:
                if exc.error_id is None:
                    raise
                errors = _append_error(errors, context.diagnostics.resolve_error(exc.error_id))
                if exc.code == "artifact_index_update_failed":
                    index_failed = True
                    break
                continue
            except Exception:
                if context.stop_signal.is_requested():
                    raise
                errors = _append_error(
                    errors,
                    context.diagnostics.record_error(
                        code="gerber_result_contract_invalid",
                        severity="error",
                        phase="branch_analysis",
                        branch="gerber",
                        message="GerberResult 或 adapter outcome 未通过完整 13 项合同校验。",
                        source_material_ids=[job_id],
                        artifact_paths=[],
                        suggested_action="检查 GerberScout output、诊断关联和字段映射。",
                    ),
                )
                continue

            result_ok, write_error, fatal_index = _write_stable(
                context,
                result,
                _result_entry(job_id, "result"),
            )
            if write_error is not None:
                errors = _append_error(errors, write_error)
            if not result_ok:
                if fatal_index:
                    index_failed = True
                    break
                continue
            summary_ok, write_error, fatal_index = _write_stable(
                context,
                adaptation.summary,
                _result_entry(job_id, "adapter_summary"),
            )
            if write_error is not None:
                errors = _append_error(errors, write_error)
            if not summary_ok:
                if fatal_index:
                    index_failed = True
                    break
                continue

            fields.extend(adaptation.fields)
            findings.extend(adaptation.findings)
            evidence.extend(adaptation.evidence)
            errors = _merge_errors(errors, adaptation.errors)

        if not fields and not index_failed:
            errors = _append_error(
                errors,
                context.diagnostics.record_error(
                    code="gerber_no_adoptable_result",
                    severity="error",
                    phase="branch_analysis",
                    branch="gerber",
                    message="Gerber 完整分析没有形成可采纳的正常字段或诊断字段。",
                    source_material_ids=source_ids,
                    artifact_paths=[],
                    suggested_action="检查层定义、GerberScout 结果和 per-job artifact。",
                ),
            )
        status = (
            BranchResultStatus.FAILED
            if index_failed or not fields
            else BranchResultStatus.COMPLETED_WITH_WARNINGS
            if findings
            or errors
            or any(item.branch_state != BranchFieldState.CLEAR_NOW for item in fields)
            else BranchResultStatus.COMPLETED
        )
        return BranchResult(
            schema_version="1.0",
            run_id=context.run_id,
            branch=BranchCode.GERBER,
            status=status,
            source_material_ids=source_ids,
            fields=fields,
            unknown_notes=[],
            tool_findings=findings,
            evidence=evidence,
            errors=errors,
        )


def _validate_inputs(snapshot, preparations, definitions):
    preparation_ids = [item.job_material_id for item in preparations]
    definition_ids = [item.job_material_id for item in definitions]
    expected = [item for item in snapshot.groups.gerber_jobs if item in set(preparation_ids)]
    if not preparations or preparation_ids != expected or definition_ids != preparation_ids:
        raise ValueError("Gerber analysis inputs must be non-empty, ordered, and one-to-one")
    if len(preparation_ids) != len(set(preparation_ids)):
        raise ValueError("Gerber analysis inputs contain duplicate jobs")
    if any(
        item.project_id != snapshot.project_id or item.run_id != snapshot.run_id
        for item in preparations
    ):
        raise ValueError("Gerber preparation identity mismatch")
    if any(
        item.project_id != snapshot.project_id
        or item.run_id != snapshot.run_id
        or item.confirmed_at is None
        for item in definitions
    ):
        raise ValueError("Gerber definition identity or confirmation mismatch")


def _validate_preparation(job: ParsedJob, preparation: GerberJobPreparation) -> None:
    expected = [
        (layer_id(item.relative_path), item.relative_path, item.detected_type)
        for item in job.layer_members
    ]
    actual = [
        (item.layer_id, item.file_path, item.detected_type.value)
        for item in preparation.layer_inputs
    ]
    if actual != expected:
        raise ValueError("Gerber preparation does not match the role-neutral cache")


def _result_entry(job_material_id: str, artifact_kind: str) -> ArtifactIndexEntry:
    filename = "result.json" if artifact_kind == "result" else "adapter_summary.json"
    return ArtifactIndexEntry(
        path=f"gerberscout/{job_material_id}/{filename}",
        kind=f"gerber_{artifact_kind}",
        producer_phase=PhaseCode.BRANCH_ANALYSIS,
        consumers=[
            ArtifactConsumer.WORKFLOW,
            ArtifactConsumer.API,
            ArtifactConsumer.GUI,
            ArtifactConsumer.REGRESSION,
        ],
        access_level=AccessLevel.PROJECT_SCOPED,
        source_material_ids=[job_material_id],
    )


def _write_stable(context, artifact, entry):
    try:
        context.artifact_writer.write_json(artifact, entry)
        return True, None, False
    except ServiceError as exc:
        if exc.error_id is None:
            raise
        error = context.diagnostics.resolve_error(exc.error_id)
        return False, error, exc.code == "artifact_index_update_failed"


def _append_error(values, error):
    if any(item.error_id == error.error_id for item in values):
        return values
    return [*values, error]


def _merge_errors(left, right):
    result = list(left)
    for error in right:
        result = _append_error(result, error)
    return result
