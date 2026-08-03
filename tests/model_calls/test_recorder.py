from __future__ import annotations

import pytest
from pydantic import ValidationError

from flyboard.model_calls import (
    CallPurpose,
    CapacityDecision,
    FailureCategory,
    ModelCallFailed,
    ModelCallSucceeded,
    TransportFailureReason,
    aggregate_model_call_summaries,
)
from flyboard.models import CallPurpose as PublicCallPurpose
from flyboard.models import ModelCallSummary


def success(purpose: CallPurpose, *, duration_ms: int = 12, retried: bool = False):
    return ModelCallSucceeded(
        call_purpose=purpose,
        policy_version="model-call-policy-v1",
        request_model_id="qwen3.7-plus-2026-05-26",
        response_model_id="qwen3.7-plus-2026-05-26",
        capacity_decision=CapacityDecision.ALLOWED,
        transport_attempt_count=2 if retried else 1,
        transport_retried=retried,
        transport_retry_reason=TransportFailureReason.TIMEOUT if retried else None,
        duration_ms=duration_ms,
        provider_request_id="req_1",
        finish_reason="stop",
        usage=None,
        response_text="restricted response",
    )


def failure(purpose: CallPurpose, *, duration_ms: int = 7):
    return ModelCallFailed(
        call_purpose=purpose,
        policy_version="model-call-policy-v1",
        request_model_id="qwen3.7-plus-2026-05-26",
        response_model_id=None,
        capacity_decision=CapacityDecision.UNKNOWN,
        transport_attempt_count=1,
        transport_retried=False,
        transport_retry_reason=None,
        duration_ms=duration_ms,
        provider_request_id=None,
        finish_reason=None,
        usage=None,
        failure_category=FailureCategory.CALL_FAILED,
        provider_code=None,
        failure_summary="transport failed",
    )


def test_call_purpose_has_one_public_owner() -> None:
    assert CallPurpose is PublicCallPurpose


def test_aggregate_inserts_by_stable_purpose_order_without_mutating_input() -> None:
    original = [
        ModelCallSummary(
            call_purpose="integration",
            logical_call_count=1,
            success_count=1,
            failure_count=0,
            total_duration_ms=20,
            transport_retry_count=0,
        )
    ]

    aggregated = aggregate_model_call_summaries(original, success(CallPurpose.XLSX))

    assert [item.call_purpose for item in aggregated] == [
        CallPurpose.XLSX,
        CallPurpose.INTEGRATION,
    ]
    assert original[0].logical_call_count == 1
    assert aggregated[1] is not original[0]


def test_aggregate_counts_success_failure_duration_and_transport_retry_once() -> None:
    summaries = aggregate_model_call_summaries(
        [], success(CallPurpose.PDF_IMAGE, duration_ms=25, retried=True)
    )
    summaries = aggregate_model_call_summaries(
        summaries, failure(CallPurpose.PDF_IMAGE, duration_ms=9)
    )

    assert summaries == [
        ModelCallSummary(
            call_purpose="pdf_image",
            logical_call_count=2,
            success_count=1,
            failure_count=1,
            total_duration_ms=34,
            transport_retry_count=1,
        )
    ]


def test_public_summary_rejects_zero_calls_bad_totals_and_excess_retries() -> None:
    base = {
        "call_purpose": "xlsx",
        "logical_call_count": 1,
        "success_count": 1,
        "failure_count": 0,
        "total_duration_ms": 0,
        "transport_retry_count": 0,
    }
    with pytest.raises(ValidationError, match="greater than 0"):
        ModelCallSummary(**{**base, "logical_call_count": 0, "success_count": 0})
    with pytest.raises(ValidationError, match="must equal"):
        ModelCallSummary(**{**base, "failure_count": 1})
    with pytest.raises(ValidationError, match="cannot exceed"):
        ModelCallSummary(**{**base, "transport_retry_count": 2})
