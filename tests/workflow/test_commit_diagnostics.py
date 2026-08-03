from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from flyboard.model_calls import (
    CallPurpose,
    CapacityDecision,
    FailureCategory,
    ModelCallFailed,
    ModelCallSucceeded,
)
from flyboard.models import (
    ArtifactIndex,
    BranchCode,
    BranchStatusMap,
    PhaseCode,
    RuleCheckSummary,
    RunStatus,
    RunSummary,
)
from flyboard.workflow.commit import CommitContractError, RunCommitChannel
from flyboard.workflow.operations import (
    DiagnosticRecorder,
    ModelCallRecorder,
    RunCommitOps,
)
from flyboard.workflow.session import RunExecutionSession

NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


def bare_summary() -> RunSummary:
    return RunSummary(
        schema_version="1.0",
        run_id="run_1",
        project_id="prj_test",
        status=RunStatus.QUEUED,
        current_phase=None,
        phase_timings=[],
        branch_status=BranchStatusMap(pdf_image="queued", xlsx="queued", gerber="queued"),
        rule_check_summary=RuleCheckSummary(
            status="skipped",
            rule_set_id=None,
            rule_set_version=None,
            attempted_rule_count=0,
            error_count=0,
            reason="rule_set_not_selected",
        ),
        model_call_summaries=[],
        error_summary=[],
        stop_requested_at=None,
        stop_reason=None,
        started_at=NOW,
        finished_at=None,
        duration_ms=None,
        artifact_index_path="artifact_index.json",
        project_data_list_path=None,
    )


class NoIoArtifacts:
    pass


def test_commit_channel_rejects_nested_entry_and_expired_context() -> None:
    channel = RunCommitChannel()
    captured = []

    def command(context):
        captured.append(context)
        with pytest.raises(CommitContractError):
            channel.execute(lambda nested: nested)
        channel.assert_context(context)

    channel.execute(command)
    with pytest.raises(CommitContractError):
        channel.assert_context(captured[0])


def test_diagnostic_recorder_reuses_exact_registered_identity() -> None:
    session = RunExecutionSession(
        project_id="prj_test",
        run_id="run_1",
        snapshot=None,
        summary=bare_summary(),
        artifact_index=ArtifactIndex(schema_version="1.0", run_id="run_1", entries=[]),
    )
    ids = iter(
        [
            UUID("00000000-0000-4000-8000-000000000001"),
            UUID("00000000-0000-4000-8000-000000000002"),
        ]
    )
    ops = RunCommitOps(session, NoIoArtifacts(), lambda: NOW, uuid_factory=ids.__next__)
    recorder = DiagnosticRecorder(session, ops)
    session.ops = ops
    session.diagnostics = recorder

    error = recorder.record_error(
        code="sample_error",
        severity="error",
        phase=None,
        branch=None,
        message="示例错误",
        source_material_ids=[],
        artifact_paths=[],
        suggested_action="重试。",
    )
    finding = recorder.record_finding(
        producer="test",
        code="sample_finding",
        kind="engineering_warning",
        message="示例发现",
        source_material_ids=[],
        evidence_ids=[],
        error_id=error.error_id,
    )

    assert recorder.resolve_error(error.error_id) is error
    assert recorder.resolve_finding(finding.finding_id) is finding
    assert error.error_id == "err_00000000-0000-4000-8000-000000000001"
    assert finding.finding_id == "finding_00000000-0000-4000-8000-000000000002"


def test_public_diagnostics_facade_fails_fast_when_called_inside_commit() -> None:
    session = RunExecutionSession(
        project_id="prj_test",
        run_id="run_1",
        snapshot=None,
        summary=bare_summary(),
        artifact_index=ArtifactIndex(schema_version="1.0", run_id="run_1", entries=[]),
    )
    ops = RunCommitOps(session, NoIoArtifacts(), lambda: NOW)
    recorder = DiagnosticRecorder(session, ops)

    def illegal_nested_call(context):
        session.channel.assert_context(context)
        with pytest.raises(CommitContractError):
            recorder.resolve_error("err_00000000-0000-4000-8000-000000000001")

    session.channel.execute(illegal_nested_call)


class RecorderArtifacts:
    def __init__(self) -> None:
        self.events = []
        self.summaries = []

    def append_trajectory(self, project_id, run_id, event) -> None:
        assert (project_id, run_id) == ("prj_test", "run_1")
        self.events.append(event)

    def replace_run_summary(self, project_id, run_id, summary) -> None:
        assert (project_id, run_id) == ("prj_test", "run_1")
        self.summaries.append(summary)


def successful_outcome() -> ModelCallSucceeded:
    return ModelCallSucceeded(
        call_purpose=CallPurpose.XLSX,
        policy_version="model-call-policy-v1",
        request_model_id="qwen3.7-plus-2026-05-26",
        response_model_id="qwen3.7-plus-2026-05-26",
        capacity_decision=CapacityDecision.ALLOWED,
        transport_attempt_count=1,
        transport_retried=False,
        transport_retry_reason=None,
        duration_ms=17,
        provider_request_id="req_1",
        finish_reason="stop",
        usage=None,
        response_text="restricted response body",
    )


def failed_outcome() -> ModelCallFailed:
    return ModelCallFailed(
        call_purpose=CallPurpose.XLSX,
        policy_version="model-call-policy-v1",
        request_model_id="qwen3.7-plus-2026-05-26",
        response_model_id=None,
        capacity_decision=CapacityDecision.TOO_LARGE,
        transport_attempt_count=0,
        transport_retried=False,
        transport_retry_reason=None,
        duration_ms=0,
        provider_request_id=None,
        finish_reason=None,
        usage=None,
        failure_category=FailureCategory.INPUT_TOO_LARGE,
        provider_code="xlsx_input_limit",
        failure_summary="request rejected by capacity preflight",
    )


def recorder_fixture():
    artifacts = RecorderArtifacts()
    session = RunExecutionSession(
        project_id="prj_test",
        run_id="run_1",
        snapshot=None,
        summary=bare_summary(),
        artifact_index=ArtifactIndex(schema_version="1.0", run_id="run_1", entries=[]),
    )
    ops = RunCommitOps(session, artifacts, lambda: NOW)
    session.ops = ops
    session.diagnostics = DiagnosticRecorder(session, ops)
    session.model_call_recorder = ModelCallRecorder(session, ops)
    return session, artifacts


def test_model_call_recorder_commits_safe_event_and_public_aggregate_once() -> None:
    session, artifacts = recorder_fixture()
    session.committed_artifact_paths.add("xlsx/model/request.json")
    outcome = successful_outcome()

    session.model_call_recorder.record(
        outcome,
        phase=PhaseCode.BRANCH_ANALYSIS,
        branch=BranchCode.XLSX,
        artifact_paths=["xlsx/model/request.json"],
    )

    assert session.summary.model_call_summaries[0].logical_call_count == 1
    assert session.summary.model_call_summaries[0].total_duration_ms == 17
    assert artifacts.summaries == [session.summary]
    assert len(artifacts.events) == 1
    event = artifacts.events[0]
    assert event.event_type == "model_response_received"
    assert event.artifact_paths == ["xlsx/model/request.json"]
    assert event.summary["call_purpose"] == "xlsx"
    assert "restricted response body" not in event.model_dump_json()

    with pytest.raises(CommitContractError, match="already recorded"):
        session.model_call_recorder.record(
            outcome,
            phase=PhaseCode.BRANCH_ANALYSIS,
            branch=BranchCode.XLSX,
            artifact_paths=["xlsx/model/request.json"],
        )
    assert session.summary.model_call_summaries[0].logical_call_count == 1
    assert len(artifacts.events) == 1


def test_model_call_recorder_validates_error_and_artifact_identity_before_commit() -> None:
    session, artifacts = recorder_fixture()
    error = session.diagnostics.record_error(
        code="xlsx_model_input_too_large",
        severity="error",
        phase="branch_analysis",
        branch="xlsx",
        message="XLSX 模型输入超过共同容量边界。",
        source_material_ids=[],
        artifact_paths=[],
        suggested_action="减少输入后创建新的运行。",
    )

    session.model_call_recorder.record(
        failed_outcome(),
        phase=PhaseCode.BRANCH_ANALYSIS,
        branch=BranchCode.XLSX,
        error_id=error.error_id,
    )

    assert session.summary.model_call_summaries[0].failure_count == 1
    assert session.summary.model_call_summaries[0].total_duration_ms == 0
    assert artifacts.events == []

    with pytest.raises(CommitContractError, match="was not committed"):
        session.model_call_recorder.record(
            successful_outcome(),
            phase=PhaseCode.BRANCH_ANALYSIS,
            branch=BranchCode.XLSX,
            artifact_paths=["xlsx/model/response.json"],
        )
    assert artifacts.events == []
    assert len(artifacts.summaries) == 1
