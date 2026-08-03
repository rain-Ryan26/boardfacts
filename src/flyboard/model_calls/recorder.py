from __future__ import annotations

from collections.abc import Sequence

from flyboard.models import CallPurpose, ModelCallSummary

from .types import ModelCallFailed, ModelCallOutcome, ModelCallSucceeded


def aggregate_model_call_summaries(
    existing: Sequence[ModelCallSummary], outcome: ModelCallOutcome
) -> list[ModelCallSummary]:
    """Return the next stable public aggregate without mutating either input."""
    if not isinstance(outcome, (ModelCallSucceeded, ModelCallFailed)):
        raise TypeError("outcome must be a closed ModelCallOutcome")

    by_purpose: dict[CallPurpose, ModelCallSummary] = {}
    observed_order: list[CallPurpose] = []
    for item in existing:
        if not isinstance(item, ModelCallSummary):
            raise TypeError("existing summaries must contain ModelCallSummary values")
        if item.call_purpose in by_purpose:
            raise ValueError("existing model call purpose must be unique")
        by_purpose[item.call_purpose] = item
        observed_order.append(item.call_purpose)

    expected_order = [purpose for purpose in CallPurpose if purpose in by_purpose]
    if observed_order != expected_order:
        raise ValueError("existing model call summaries are not in stable purpose order")

    previous = by_purpose.get(outcome.call_purpose)
    logical_call_count = 1
    success_count = int(isinstance(outcome, ModelCallSucceeded))
    failure_count = int(isinstance(outcome, ModelCallFailed))
    total_duration_ms = outcome.duration_ms
    transport_retry_count = int(outcome.transport_retried)
    if previous is not None:
        logical_call_count += previous.logical_call_count
        success_count += previous.success_count
        failure_count += previous.failure_count
        total_duration_ms += previous.total_duration_ms
        transport_retry_count += previous.transport_retry_count

    by_purpose[outcome.call_purpose] = ModelCallSummary(
        call_purpose=outcome.call_purpose,
        logical_call_count=logical_call_count,
        success_count=success_count,
        failure_count=failure_count,
        total_duration_ms=total_duration_ms,
        transport_retry_count=transport_retry_count,
    )
    return [
        by_purpose[purpose].model_copy(deep=True)
        for purpose in CallPurpose
        if purpose in by_purpose
    ]
